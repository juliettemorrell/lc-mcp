"""
Longevity Copilot MCP Server (Streamable HTTP) - hardened build.

What this version adds beyond v1.0:
  - Bearer-token auth (optional via MCP_BEARER_TOKEN env var)
  - Structured audit logging (request_id, ts, tool, latency_ms, status)
  - httpx retries with exponential backoff for the FHIR proxy
  - 60+ vendor->LOINC mappings (full longevity panel coverage)
  - Three real clinical calculators: HOMA-IR, eGFR (CKD-EPI 2021), ASCVD 10-yr risk
  - RxNav drug-interaction lookup
  - CORS preflight handler for browser-based Po extensions
  - Health check with dependency status

Tools exposed (12):
  - get_patient_demographics
  - get_patient_labs
  - get_patient_medications
  - get_patient_genomics
  - get_wearable_snapshot
  - normalize_biomarker
  - fhir_passthrough
  - calc_homa_ir
  - calc_egfr_ckdepi_2021
  - calc_ascvd_10yr
  - rxnav_interactions
  - audit_tail

Run locally:  uvicorn server:app --host 0.0.0.0 --port 8080
Auth:         MCP_BEARER_TOKEN=secret uvicorn server:app ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

# Lazy import of reportlab so the server boots even if the dep is missing.
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    REPORTLAB_OK = True
except Exception:  # noqa: BLE001
    REPORTLAB_OK = False

# Matplotlib for chart_lab_trend.
try:
    import matplotlib
    matplotlib.use("Agg")  # no display
    import matplotlib.pyplot as plt
    MATPLOTLIB_OK = True
except Exception:  # noqa: BLE001
    MATPLOTLIB_OK = False

# ---------------------------------------------------------------------------
# Structured logging. JSON lines, one per call. In production pipe to stdout
# and let your platform collect it (Render Logs / Fly Logs / CloudWatch).
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
)
log = logging.getLogger("lc-mcp")

# Prometheus-style per-tool counters. Maps tool name -> {calls, errors, total_ms}.
METRICS: dict[str, dict[str, int]] = {}


def metrics_record(name: str, latency_ms: int, err: bool) -> None:
    m = METRICS.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0})
    m["calls"] += 1
    m["total_ms"] += latency_ms
    if err:
        m["errors"] += 1


# In-memory audit ring (last 200 events) + append-only file (HIPAA-style trail).
# Set AUDIT_LOG_PATH to enable durable storage. Default: /tmp/lc-audit.ndjson.
AUDIT_RING: deque[dict[str, Any]] = deque(maxlen=200)
AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "/tmp/lc-audit.ndjson")


def audit(request_id: str, kind: str, **fields: Any) -> None:
    """Emit a structured audit event to stdout, in-memory ring, and append-only file."""
    event = {
        "request_id": request_id,
        "ts": int(time.time() * 1000),
        "kind": kind,
        **fields,
    }
    AUDIT_RING.append(event)
    line = json.dumps(event, default=str)
    log.info(line)
    # Append-only persistence. Best-effort: a write failure must not break the call.
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Auth: bearer token via env. When unset, server runs open (dev mode).
# ---------------------------------------------------------------------------

MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "").strip()


def require_auth(authorization: str | None, request_id: str) -> None:
    """Reject if a bearer token is configured and the header doesn't match."""
    if not MCP_BEARER_TOKEN:
        return
    expected = f"Bearer {MCP_BEARER_TOKEN}"
    if (authorization or "").strip() != expected:
        audit(request_id, "auth.deny", reason="bearer-mismatch")
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Vendor source registries. Expanded coverage across the longevity ecosystem.
# ---------------------------------------------------------------------------

LAB_VENDORS = [
    "Quest Diagnostics", "LabCorp", "Boston Heart", "Genova Diagnostics",
    "Diagnostic Solutions", "Doctor's Data", "Vibrant America", "ZRT Laboratory",
    "Cleveland HeartLab", "Spectracell", "Dutch (Precision Analytical)", "Cyrex Labs",
    "Mosaic Diagnostics", "Great Plains Laboratory", "BioReference Health",
    "Empire City Labs", "Walk-In Lab", "Function Health",
]

# Wearables / connected devices with public or partner APIs. Includes rings,
# watches, CGMs, BP cuffs, ECG patches, smart scales, fertility monitors,
# CPAP, smart beds, and aggregators that fan out to many devices.
WEARABLE_SOURCES = [
    # Rings
    "Oura", "Ultrahuman Ring AIR", "RingConn", "Movano Evie Ring", "Circul+ Ring",
    # Watches / straps
    "Whoop", "Apple Health (HealthKit)", "Garmin Connect", "Fitbit Web API", "Polar Flow",
    "Suunto Cloud", "Coros", "Wahoo Cloud", "Samsung Health",
    # Phone / platform aggregators
    "Google Fit", "Android Health Connect", "Strava",
    # CGMs
    "Dexcom G6/G7", "FreeStyle Libre", "Levels", "Veri", "Nutrisense", "January AI",
    # BP / cardiac
    "Withings", "Omron Connect", "QardioArm / QardioCore", "iHealth", "KardiaMobile (AliveCor)",
    # SpO2 / overnight
    "Wellue O2Ring", "Masimo SafetyNet", "Owlet Dream Sock",
    # Smart beds + sleep
    "Eight Sleep", "Sleep Number", "Beddit",
    # Respiratory
    "ResMed AirView", "Spiroo", "NuvoAir",
    # Fertility
    "Tempdrop", "Mira", "Inito", "Ava",
    # Smart scales
    "Renpho", "Withings Body+",
    # ECG / digital stethoscope
    "Eko CORE", "Bittium Faros", "Movesense",
    # Research-grade
    "Empatica EmbracePlus", "Hexoskin", "Biostrap",
    # Aggregators (one wire, many devices)
    "Terra API", "Validic", "Rook", "Human API", "Spike API", "Healthie",
]

EHR_SOURCES = [
    "HAPI FHIR", "SMART Health IT", "Epic R4", "Cerner / Oracle Health",
    "AthenaHealth", "MEDITECH Expanse", "Allscripts / Veradigm", "NextGen",
    "eClinicalWorks", "Practice Fusion", "Drchrono", "Kareo",
    "InterSystems IRIS for Health", "Greenway Intergy", "Azalea Health",
    "Open mHealth", "Particle Health", "Health Gorilla", "1up.health",
]

# Per-device API notes for the agent. Tells the agent what auth + endpoint
# shape to expect. Used by list_supported_sources for richer discovery.
WEARABLE_API_DETAILS: dict[str, dict[str, str]] = {
    "Oura":                       {"auth": "OAuth 2.0",   "base": "https://api.ouraring.com/v2/",                  "metrics": "HRV (RMSSD), RHR, sleep stages, SpO2, body temp deviation"},
    "Ultrahuman Ring AIR":        {"auth": "OAuth 2.0",   "base": "https://api.ultrahuman.com/v1/",                "metrics": "HRV, RHR, sleep, glucose (CGM-paired)"},
    "RingConn":                   {"auth": "OAuth 2.0",   "base": "partner SDK",                                    "metrics": "HRV, RHR, SpO2, sleep, stress"},
    "Movano Evie Ring":           {"auth": "Partner API", "base": "partner",                                        "metrics": "HRV, RHR, SpO2, menstrual"},
    "Whoop":                      {"auth": "OAuth 2.0",   "base": "https://api.prod.whoop.com/developer/",         "metrics": "Strain, recovery, HRV, sleep stages, RHR"},
    "Apple Health (HealthKit)":   {"auth": "User opt-in", "base": "iOS HealthKit",                                  "metrics": "Steps, active kcal, HRV (SDNN), VO2max, ECG, mobility"},
    "Garmin Connect":             {"auth": "OAuth 1.0a",  "base": "https://apis.garmin.com/wellness-api/rest/",    "metrics": "HRV, RHR, sleep, training load, body battery"},
    "Fitbit Web API":             {"auth": "OAuth 2.0",   "base": "https://api.fitbit.com/1/",                     "metrics": "Steps, HR, SpO2, sleep, weight"},
    "Polar Flow":                 {"auth": "OAuth 2.0",   "base": "https://www.polaraccesslink.com/v3/",           "metrics": "HR, training, sleep, recovery"},
    "Suunto Cloud":               {"auth": "OAuth 2.0",   "base": "https://cloudapi.suunto.com/v2/",               "metrics": "HR, GPS, altimetry, sleep"},
    "Coros":                      {"auth": "Partner API", "base": "partner",                                        "metrics": "HR, GPS, training"},
    "Wahoo Cloud":                {"auth": "OAuth 2.0",   "base": "https://cloud-api.wahooligan.com/v1/",          "metrics": "HR, power, cadence, GPS"},
    "Samsung Health":             {"auth": "Partner SDK", "base": "Samsung Health SDK",                             "metrics": "HR, sleep, activity, glucose (via partner)"},
    "Google Fit":                 {"auth": "OAuth 2.0",   "base": "https://www.googleapis.com/fitness/v1/",        "metrics": "Steps, HR, weight, nutrition"},
    "Android Health Connect":     {"auth": "Local",       "base": "Android SDK",                                    "metrics": "All Android health data, on-device"},
    "Strava":                     {"auth": "OAuth 2.0",   "base": "https://www.strava.com/api/v3/",                "metrics": "Activities, HR streams"},
    "Dexcom G6/G7":               {"auth": "OAuth 2.0",   "base": "https://api.dexcom.com/v3/users/self/",         "metrics": "Glucose (5-min interval), trend arrows"},
    "FreeStyle Libre":            {"auth": "Partner via LibreView", "base": "https://api.libreview.io/llu/",        "metrics": "Glucose (15-min), trend"},
    "Levels":                     {"auth": "Partner API", "base": "partner",                                        "metrics": "Glucose, food-glucose correlation"},
    "Veri":                       {"auth": "Partner API", "base": "partner",                                        "metrics": "Glucose, metabolic score"},
    "Nutrisense":                 {"auth": "Partner API", "base": "partner",                                        "metrics": "Glucose, food logging"},
    "January AI":                 {"auth": "Partner API", "base": "partner",                                        "metrics": "Glucose, AI insights"},
    "Withings":                   {"auth": "OAuth 2.0",   "base": "https://wbsapi.withings.net/",                  "metrics": "Weight, BP, HR, sleep, ECG"},
    "Omron Connect":              {"auth": "OAuth 2.0",   "base": "https://api.omronconnect.com/",                 "metrics": "BP, HR, weight"},
    "QardioArm / QardioCore":     {"auth": "Partner API", "base": "partner",                                        "metrics": "BP, ECG, HR"},
    "iHealth":                    {"auth": "OAuth 2.0",   "base": "https://api.ihealthlabs.com/",                  "metrics": "BP, glucose, SpO2, weight"},
    "KardiaMobile (AliveCor)":    {"auth": "Partner API", "base": "partner",                                        "metrics": "Single-lead ECG, AFib detection"},
    "Wellue O2Ring":              {"auth": "Local",       "base": "ViHealth app + CSV export",                      "metrics": "Continuous SpO2 + HR overnight"},
    "Masimo SafetyNet":           {"auth": "Partner API", "base": "partner",                                        "metrics": "Continuous SpO2, perfusion index"},
    "Owlet Dream Sock":           {"auth": "Partner API", "base": "partner",                                        "metrics": "Infant SpO2, HR"},
    "Eight Sleep":                {"auth": "Partner API", "base": "partner",                                        "metrics": "Sleep stages, HRV, RHR, body temp regulation"},
    "Sleep Number":               {"auth": "Partner API", "base": "partner",                                        "metrics": "SleepIQ score, sleep stages, restful sleep %"},
    "Beddit":                     {"auth": "Apple Health only", "base": "via HealthKit",                            "metrics": "Sleep, RHR, snoring"},
    "ResMed AirView":             {"auth": "Partner / OAuth", "base": "https://airview.resmed.com/",               "metrics": "AHI, mask leak, hours used (CPAP)"},
    "Spiroo":                     {"auth": "Partner API", "base": "partner",                                        "metrics": "FEV1, FVC, peak flow"},
    "NuvoAir":                    {"auth": "Partner API", "base": "partner",                                        "metrics": "Spirometry"},
    "Tempdrop":                   {"auth": "Partner API", "base": "partner",                                        "metrics": "Basal body temperature overnight"},
    "Mira":                       {"auth": "Partner API", "base": "partner",                                        "metrics": "LH, FSH, E3G urine hormones"},
    "Inito":                      {"auth": "Partner API", "base": "partner",                                        "metrics": "LH, FSH, PdG, E3G"},
    "Ava":                        {"auth": "Partner API", "base": "partner",                                        "metrics": "Skin temp, HRV, breathing rate (fertility)"},
    "Renpho":                     {"auth": "Partner API", "base": "partner",                                        "metrics": "Weight, body composition"},
    "Withings Body+":             {"auth": "OAuth 2.0",   "base": "https://wbsapi.withings.net/",                  "metrics": "Weight, body fat, muscle mass"},
    "Eko CORE":                   {"auth": "Partner API", "base": "partner",                                        "metrics": "Digital stethoscope audio, ECG"},
    "Bittium Faros":              {"auth": "Local",       "base": "device export",                                  "metrics": "Holter ECG (clinical-grade)"},
    "Movesense":                  {"auth": "Open SDK",    "base": "https://www.movesense.com/developers/",         "metrics": "ECG, HR, motion (research)"},
    "Empatica EmbracePlus":       {"auth": "FDA-cleared API", "base": "https://www.empatica.com/connect/",         "metrics": "HR, EDA, accelerometry, temp (medical-grade)"},
    "Hexoskin":                   {"auth": "OAuth 2.0",   "base": "https://api.hexoskin.com/",                     "metrics": "HR, breathing, sleep (smart shirt)"},
    "Biostrap":                   {"auth": "Partner API", "base": "partner",                                        "metrics": "HRV, SpO2, RHR, sleep"},
    "Circul+ Ring":               {"auth": "Partner API", "base": "partner",                                        "metrics": "SpO2, HRV, HR overnight"},
    "Terra API":                  {"auth": "API Key",     "base": "https://api.tryterra.co/v2/",                   "metrics": "Aggregator: 100+ devices via one API"},
    "Validic":                    {"auth": "API Key",     "base": "https://api.validic.com/v2/",                   "metrics": "Aggregator: clinical-grade device normalization"},
    "Rook":                       {"auth": "API Key",     "base": "https://api.rook-connect.com/",                 "metrics": "Aggregator: 200+ wearables + EHR data"},
    "Human API":                  {"auth": "OAuth 2.0",   "base": "https://api.humanapi.co/v1/",                   "metrics": "Aggregator: medical + wearable"},
    "Spike API":                  {"auth": "API Key",     "base": "https://api.spikeapi.com/",                     "metrics": "Aggregator: lightweight"},
    "Healthie":                   {"auth": "Partner API", "base": "https://api.gethealthie.com/",                  "metrics": "EHR-side wearable bridge"},
}

# Canonical wearable metric names. Vendor metric -> canonical key.
# When a vendor returns "rmssd_ms" or "hrv_rmssd" or "heart_rate_variability_avg",
# we normalize to "hrv_rmssd_ms_mean".
WEARABLE_METRIC_MAP: dict[str, str] = {
    "oura:hrv_rmssd":             "hrv_rmssd_ms_mean",
    "oura:resting_heart_rate":    "rhr_bpm_mean",
    "oura:deep_sleep":            "deep_sleep_min_mean",
    "oura:rem_sleep":              "rem_sleep_min_mean",
    "oura:spo2_average":           "spo2_pct_min_overnight",
    "whoop:strain":                "strain_mean",
    "whoop:recovery":              "recovery_pct_mean",
    "whoop:hrv":                   "hrv_rmssd_ms_mean",
    "garmin:rest_hr":              "rhr_bpm_mean",
    "garmin:body_battery":         "body_battery_mean",
    "apple_health:hrv_sdnn":       "hrv_sdnn_ms_mean",
    "apple_health:vo2_max":        "vo2_max",
    "fitbit:resting_hr":           "rhr_bpm_mean",
    "withings:weight":             "weight_kg",
    "withings:bp_systolic":        "systolic_bp_mmHg",
    "withings:bp_diastolic":       "diastolic_bp_mmHg",
    "dexcom:glucose":              "glucose_mg_dL",
    "libre:glucose":               "glucose_mg_dL",
    "levels:glucose":              "glucose_mg_dL",
    "wellue:spo2":                 "spo2_pct_mean",
    "kardia:ecg_lead_i":           "ecg_lead_i",
    "omron:bp_systolic":           "systolic_bp_mmHg",
    "omron:bp_diastolic":          "diastolic_bp_mmHg",
    "resmed:ahi":                  "apnea_hypopnea_index",
    "tempdrop:bbt_c":              "basal_body_temp_c",
    "mira:lh_miu_mL":              "luteinizing_hormone_miu_mL",
    "mira:fsh_miu_mL":             "follicle_stimulating_hormone_miu_mL",
    "eight_sleep:sleep_score":     "sleep_score_mean",
}

# Vendor -> LOINC map. Full longevity panel: CBC, CMP, lipid + ApoB/Lp(a),
# HbA1c + insulin, thyroid (TSH/T3/T4/rT3/TPO/Tg), sex hormones (testosterone,
# estradiol, progesterone, DHEA, cortisol diurnal), inflammation (hsCRP, hcy,
# ferritin), vitamins (D, B12, folate, K2), minerals (Mg, Zn, Cu, Se),
# methylation, heavy metals, omega index, IGF-1, autoimmune.

# fmt: off
VENDOR_TO_LOINC: dict[str, dict[str, str]] = {
    # ---- Glycemic ----
    "quest:HBA1C":                {"loinc": "4548-4",  "ucum": "%",       "name": "Hemoglobin A1c"},
    "labcorp:HEMOGLOBIN_A1C":     {"loinc": "4548-4",  "ucum": "%",       "name": "Hemoglobin A1c"},
    "bostonheart:A1C":            {"loinc": "4548-4",  "ucum": "%",       "name": "Hemoglobin A1c"},
    "vibrant:HBA1C":              {"loinc": "4548-4",  "ucum": "%",       "name": "Hemoglobin A1c"},
    "quest:FASTING_GLUCOSE":      {"loinc": "1558-6",  "ucum": "mg/dL",   "name": "Fasting glucose"},
    "labcorp:GLUCOSE_FASTING":    {"loinc": "1558-6",  "ucum": "mg/dL",   "name": "Fasting glucose"},
    "quest:FASTING_INSULIN":      {"loinc": "1554-5",  "ucum": "uIU/mL",  "name": "Fasting insulin"},
    "labcorp:INSULIN_FASTING":    {"loinc": "1554-5",  "ucum": "uIU/mL",  "name": "Fasting insulin"},
    "quest:C_PEPTIDE":            {"loinc": "1986-9",  "ucum": "ng/mL",   "name": "C-peptide"},
    "quest:FRUCTOSAMINE":         {"loinc": "1558-6",  "ucum": "umol/L",  "name": "Fructosamine"},
    # ---- Lipids ----
    "quest:LDL_C":                {"loinc": "13457-7", "ucum": "mg/dL",   "name": "LDL cholesterol"},
    "labcorp:LDL":                {"loinc": "13457-7", "ucum": "mg/dL",   "name": "LDL cholesterol"},
    "quest:HDL_C":                {"loinc": "2085-9",  "ucum": "mg/dL",   "name": "HDL cholesterol"},
    "labcorp:HDL":                {"loinc": "2085-9",  "ucum": "mg/dL",   "name": "HDL cholesterol"},
    "quest:TRIGLYCERIDES":        {"loinc": "2571-8",  "ucum": "mg/dL",   "name": "Triglycerides"},
    "labcorp:TRIG":               {"loinc": "2571-8",  "ucum": "mg/dL",   "name": "Triglycerides"},
    "quest:TOTAL_CHOLESTEROL":    {"loinc": "2093-3",  "ucum": "mg/dL",   "name": "Total cholesterol"},
    "labcorp:CHOL_TOTAL":         {"loinc": "2093-3",  "ucum": "mg/dL",   "name": "Total cholesterol"},
    "quest:APOLIPOPROTEIN_B":     {"loinc": "1884-6",  "ucum": "mg/dL",   "name": "Apolipoprotein B"},
    "labcorp:APO_B":              {"loinc": "1884-6",  "ucum": "mg/dL",   "name": "Apolipoprotein B"},
    "bostonheart:APOB":           {"loinc": "1884-6",  "ucum": "mg/dL",   "name": "Apolipoprotein B"},
    "quest:LIPOPROTEIN_A":        {"loinc": "10835-7", "ucum": "nmol/L",  "name": "Lipoprotein(a)"},
    "labcorp:LP_A":               {"loinc": "10835-7", "ucum": "nmol/L",  "name": "Lipoprotein(a)"},
    "bostonheart:LP_A":           {"loinc": "10835-7", "ucum": "nmol/L",  "name": "Lipoprotein(a)"},
    "clevelandheart:LDL_P":       {"loinc": "54434-2", "ucum": "nmol/L",  "name": "LDL-P particle count"},
    "clevelandheart:SDLDL":       {"loinc": "11054-4", "ucum": "mg/dL",   "name": "Small dense LDL"},
    "bostonheart:OMEGA3_INDEX":   {"loinc": "73810-3", "ucum": "%",       "name": "Omega-3 Index"},
    # ---- Thyroid ----
    "quest:TSH":                  {"loinc": "3016-3",  "ucum": "mIU/L",   "name": "Thyrotropin"},
    "labcorp:THYROID_STIMULATING":{"loinc": "3016-3",  "ucum": "mIU/L",   "name": "Thyrotropin"},
    "vibrant:TSH":                {"loinc": "3016-3",  "ucum": "mIU/L",   "name": "Thyrotropin"},
    "quest:FREE_T3":              {"loinc": "3051-0",  "ucum": "pg/mL",   "name": "Free T3"},
    "labcorp:FREE_T3":            {"loinc": "3051-0",  "ucum": "pg/mL",   "name": "Free T3"},
    "quest:FREE_T4":              {"loinc": "3024-7",  "ucum": "ng/dL",   "name": "Free T4"},
    "labcorp:FREE_T4":            {"loinc": "3024-7",  "ucum": "ng/dL",   "name": "Free T4"},
    "quest:TOTAL_T3":             {"loinc": "3053-6",  "ucum": "ng/dL",   "name": "Total T3"},
    "quest:TOTAL_T4":             {"loinc": "3026-2",  "ucum": "ug/dL",   "name": "Total T4"},
    "quest:REVERSE_T3":           {"loinc": "30097-5", "ucum": "ng/dL",   "name": "Reverse T3"},
    "quest:TPO_AB":               {"loinc": "8099-1",  "ucum": "IU/mL",   "name": "Thyroid peroxidase antibody"},
    "labcorp:ANTI_TPO":           {"loinc": "8099-1",  "ucum": "IU/mL",   "name": "Thyroid peroxidase antibody"},
    "quest:THYROGLOBULIN_AB":     {"loinc": "8095-9",  "ucum": "IU/mL",   "name": "Thyroglobulin antibody"},
    # ---- Sex hormones ----
    "quest:TESTOSTERONE_TOTAL":   {"loinc": "2986-8",  "ucum": "ng/dL",   "name": "Total testosterone"},
    "labcorp:TESTO_TOTAL":        {"loinc": "2986-8",  "ucum": "ng/dL",   "name": "Total testosterone"},
    "quest:TESTOSTERONE_FREE":    {"loinc": "2991-8",  "ucum": "pg/mL",   "name": "Free testosterone"},
    "labcorp:TESTO_FREE":         {"loinc": "2991-8",  "ucum": "pg/mL",   "name": "Free testosterone"},
    "quest:SHBG":                 {"loinc": "13967-5", "ucum": "nmol/L",  "name": "Sex hormone binding globulin"},
    "quest:ESTRADIOL":            {"loinc": "14715-7", "ucum": "pg/mL",   "name": "Estradiol"},
    "quest:PROGESTERONE":         {"loinc": "2839-9",  "ucum": "ng/mL",   "name": "Progesterone"},
    "quest:PREGNENOLONE":         {"loinc": "2638-5",  "ucum": "ng/dL",   "name": "Pregnenolone"},
    "quest:DHEA_SULFATE":         {"loinc": "2191-5",  "ucum": "ug/dL",   "name": "DHEA-Sulfate"},
    "quest:DHEA":                 {"loinc": "2197-2",  "ucum": "ng/dL",   "name": "DHEA"},
    "zrt:ESTRADIOL_SALIVARY":     {"loinc": "14715-7", "ucum": "pg/mL",   "name": "Salivary estradiol"},
    "dutch:CORTISOL_AM":          {"loinc": "2143-6",  "ucum": "ug/dL",   "name": "Cortisol AM"},
    "dutch:CORTISOL_PM":          {"loinc": "2143-6",  "ucum": "ug/dL",   "name": "Cortisol PM"},
    "dutch:CORTISOL_DIURNAL":     {"loinc": "2143-6",  "ucum": "ug/dL",   "name": "Cortisol diurnal pattern"},
    # ---- Inflammation ----
    "quest:HS_CRP":               {"loinc": "30522-7", "ucum": "mg/L",    "name": "High-sensitivity CRP"},
    "labcorp:HS_CRP":             {"loinc": "30522-7", "ucum": "mg/L",    "name": "High-sensitivity CRP"},
    "vibrant:HS_CRP":             {"loinc": "30522-7", "ucum": "mg/L",    "name": "High-sensitivity CRP"},
    "quest:HOMOCYSTEINE":         {"loinc": "13965-9", "ucum": "umol/L",  "name": "Homocysteine"},
    "labcorp:HOMOCYS":            {"loinc": "13965-9", "ucum": "umol/L",  "name": "Homocysteine"},
    "bostonheart:HCY":            {"loinc": "13965-9", "ucum": "umol/L",  "name": "Homocysteine"},
    "quest:FERRITIN":             {"loinc": "2276-4",  "ucum": "ng/mL",   "name": "Ferritin"},
    "labcorp:FERRITIN":           {"loinc": "2276-4",  "ucum": "ng/mL",   "name": "Ferritin"},
    "bostonheart:TMAO":           {"loinc": "72090-3", "ucum": "uM",      "name": "Trimethylamine N-oxide"},
    "bostonheart:ADMA":           {"loinc": "72091-1", "ucum": "umol/L",  "name": "Asymmetric dimethylarginine"},
    # ---- Vitamins / nutrient status ----
    "quest:VITAMIN_D_25":         {"loinc": "62292-8", "ucum": "ng/mL",   "name": "25-OH Vitamin D"},
    "labcorp:VIT_D_25_OH":        {"loinc": "62292-8", "ucum": "ng/mL",   "name": "25-OH Vitamin D"},
    "quest:VITAMIN_B12":          {"loinc": "2132-9",  "ucum": "pg/mL",   "name": "Vitamin B12"},
    "labcorp:B12":                {"loinc": "2132-9",  "ucum": "pg/mL",   "name": "Vitamin B12"},
    "quest:FOLATE_SERUM":         {"loinc": "2284-8",  "ucum": "ng/mL",   "name": "Folate serum"},
    "quest:FOLATE_RBC":           {"loinc": "2286-3",  "ucum": "ng/mL",   "name": "RBC folate"},
    "quest:VITAMIN_K2":           {"loinc": "17856-6", "ucum": "ng/mL",   "name": "Vitamin K2"},
    "quest:VITAMIN_A":            {"loinc": "2923-1",  "ucum": "ug/dL",   "name": "Vitamin A"},
    "quest:VITAMIN_E":            {"loinc": "1823-4",  "ucum": "mg/L",    "name": "Vitamin E"},
    "quest:METHYLMALONIC_ACID":   {"loinc": "1759-0",  "ucum": "nmol/L",  "name": "Methylmalonic acid (MMA)"},
    "quest:RBC_MAGNESIUM":        {"loinc": "11218-5", "ucum": "mg/dL",   "name": "RBC magnesium"},
    "spectracell:RBC_ZINC":       {"loinc": "5763-6",  "ucum": "ug/dL",   "name": "RBC zinc"},
    "spectracell:RBC_COPPER":     {"loinc": "5631-5",  "ucum": "ug/dL",   "name": "RBC copper"},
    "spectracell:RBC_SELENIUM":   {"loinc": "5697-6",  "ucum": "ug/L",    "name": "RBC selenium"},
    "spectracell:GLUTATHIONE":    {"loinc": "17935-8", "ucum": "ug/mL",   "name": "Glutathione"},
    "spectracell:COQ10":          {"loinc": "73969-7", "ucum": "ug/mL",   "name": "Coenzyme Q10"},
    "quest:IGF_1":                {"loinc": "2484-4",  "ucum": "ng/mL",   "name": "IGF-1"},
    # ---- Heavy metals / toxins ----
    "doctorsdata:MERCURY_WHOLE":  {"loinc": "5685-1",  "ucum": "ug/L",    "name": "Mercury whole blood"},
    "doctorsdata:LEAD_WHOLE":     {"loinc": "5671-3",  "ucum": "ug/dL",   "name": "Lead whole blood"},
    "doctorsdata:ARSENIC_URINE":  {"loinc": "5588-3",  "ucum": "ug/g",    "name": "Arsenic urine"},
    "doctorsdata:CADMIUM_URINE":  {"loinc": "5611-3",  "ucum": "ug/g",    "name": "Cadmium urine"},
    # ---- Autoimmune ----
    "quest:ANA":                  {"loinc": "5048-4",  "ucum": "{titer}", "name": "Antinuclear antibody"},
    "labcorp:RF":                 {"loinc": "11572-5", "ucum": "IU/mL",   "name": "Rheumatoid factor"},
    "labcorp:ANTI_CCP":           {"loinc": "32218-0", "ucum": "U/mL",    "name": "Anti-CCP"},
    # ---- Renal / hepatic (CMP) ----
    "quest:CREATININE":           {"loinc": "2160-0",  "ucum": "mg/dL",   "name": "Creatinine"},
    "labcorp:CR":                 {"loinc": "2160-0",  "ucum": "mg/dL",   "name": "Creatinine"},
    "quest:EGFR":                 {"loinc": "33914-3", "ucum": "mL/min/1.73m2", "name": "eGFR"},
    "quest:BUN":                  {"loinc": "3094-0",  "ucum": "mg/dL",   "name": "Blood urea nitrogen"},
    "quest:ALT":                  {"loinc": "1742-6",  "ucum": "U/L",     "name": "ALT"},
    "quest:AST":                  {"loinc": "1920-8",  "ucum": "U/L",     "name": "AST"},
    "quest:GGT":                  {"loinc": "2324-2",  "ucum": "U/L",     "name": "GGT"},
    "quest:ALKALINE_PHOSPHATASE": {"loinc": "6768-6",  "ucum": "U/L",     "name": "Alkaline phosphatase"},
    "quest:POTASSIUM":            {"loinc": "2823-3",  "ucum": "mmol/L",  "name": "Potassium"},
    "quest:SODIUM":               {"loinc": "2951-2",  "ucum": "mmol/L",  "name": "Sodium"},
    "quest:CHLORIDE":             {"loinc": "2075-0",  "ucum": "mmol/L",  "name": "Chloride"},
    "quest:CO2":                  {"loinc": "2028-9",  "ucum": "mmol/L",  "name": "CO2"},
    "quest:CALCIUM":              {"loinc": "17861-6", "ucum": "mg/dL",   "name": "Calcium"},
    # ---- Iron studies ----
    "quest:IRON":                 {"loinc": "2498-4",  "ucum": "ug/dL",   "name": "Iron"},
    "quest:TIBC":                 {"loinc": "2500-7",  "ucum": "ug/dL",   "name": "Total iron binding capacity"},
    "quest:TRANSFERRIN_SAT":      {"loinc": "2502-3",  "ucum": "%",       "name": "Transferrin saturation"},
    # ---- Gut / microbiome (Genova GI Effects, Diagnostic Solutions GI-MAP) ----
    "genova:CALPROTECTIN":        {"loinc": "38445-3", "ucum": "ug/g",    "name": "Fecal calprotectin"},
    "genova:SECRETORY_IGA":       {"loinc": "53929-2", "ucum": "ug/mL",   "name": "Secretory IgA stool"},
    "diagnosticsolutions:ZONULIN":{"loinc": "82193-0", "ucum": "ng/mL",   "name": "Zonulin"},
    # ---- Dutch (Precision Analytical) - urinary hormone metabolites ----
    "dutch:ESTRONE_E1":           {"loinc": "13561-6", "ucum": "ng/mg",   "name": "Estrone (E1)"},
    "dutch:ESTRIOL_E3":            {"loinc": "12251-5", "ucum": "ng/mg",   "name": "Estriol (E3)"},
    "dutch:2_OH_E1":               {"loinc": "33815-2", "ucum": "ng/mg",   "name": "2-OH-Estrone"},
    "dutch:4_OH_E1":               {"loinc": "33816-0", "ucum": "ng/mg",   "name": "4-OH-Estrone"},
    "dutch:16_OH_E1":              {"loinc": "33817-8", "ucum": "ng/mg",   "name": "16-OH-Estrone"},
    "dutch:CORTISOL_FREE":         {"loinc": "2143-6",  "ucum": "ng/mg",   "name": "Free cortisol (urinary)"},
    "dutch:CORTISONE":             {"loinc": "2147-7",  "ucum": "ng/mg",   "name": "Cortisone"},
    "dutch:5A_THF":                {"loinc": "47194-3", "ucum": "ng/mg",   "name": "5alpha-THF"},
    "dutch:5B_THF":                {"loinc": "47193-5", "ucum": "ng/mg",   "name": "5beta-THF"},
    "dutch:6_OH_MELATONIN":        {"loinc": "55478-3", "ucum": "ng/mg",   "name": "6-OH Melatonin sulfate"},
    # ---- Cyrex (food sensitivity / autoimmune cross-reactivity) ----
    "cyrex:GLUTEN_IgG":            {"loinc": "30994-3", "ucum": "IU/mL",   "name": "Gluten IgG"},
    "cyrex:GLUTEN_IgA":            {"loinc": "30995-0", "ucum": "IU/mL",   "name": "Gluten IgA"},
    "cyrex:DAIRY_IgG":             {"loinc": "9352-1",  "ucum": "IU/mL",   "name": "Dairy IgG"},
    "cyrex:WHEAT_GERM_AGGLUTININ": {"loinc": "33796-4", "ucum": "IU/mL",   "name": "Wheat germ agglutinin Ab"},
    # ---- Spectracell additional micronutrients ----
    "spectracell:RBC_CALCIUM":     {"loinc": "1995-0",  "ucum": "mg/dL",   "name": "RBC calcium"},
    "spectracell:VITAMIN_B6":      {"loinc": "2287-1",  "ucum": "nmol/L",  "name": "Vitamin B6 (pyridoxine)"},
    "spectracell:CARNITINE":       {"loinc": "1990-1",  "ucum": "umol/L",  "name": "Carnitine"},
    "spectracell:ALPHA_LIPOIC":    {"loinc": "73974-7", "ucum": "ug/mL",   "name": "Alpha-lipoic acid"},
    # ---- Genova GI Effects (stool panel) ----
    "genova:STOOL_BACTERIA":       {"loinc": "76486-6", "ucum": "CFU/g",   "name": "Stool beneficial bacteria"},
    "genova:STOOL_PARASITES":      {"loinc": "76486-7", "ucum": "{detect}","name": "Stool parasites"},
    "genova:SECRETORY_CHROMOGRAN": {"loinc": "70957-5", "ucum": "ng/g",    "name": "Stool chromogranin A"},
    "genova:STOOL_ELASTASE":       {"loinc": "73992-9", "ucum": "ug/g",    "name": "Pancreatic elastase 1"},
    # ---- Mosaic Diagnostics (organic acids, OAT panel) ----
    "mosaic:KYNURENATE":           {"loinc": "46603-4", "ucum": "ug/mg",   "name": "Kynurenate (OAT)"},
    "mosaic:QUINOLINATE":          {"loinc": "46604-2", "ucum": "ug/mg",   "name": "Quinolinate (OAT)"},
    "mosaic:8_OH_GUANOSINE":       {"loinc": "73975-4", "ucum": "ug/mg",   "name": "8-OH-2-deoxyguanosine (oxidative stress)"},
    # ---- Boston Heart cardiology extras ----
    "bostonheart:MPO":             {"loinc": "73796-2", "ucum": "pmol/L",  "name": "Myeloperoxidase (MPO)"},
    "bostonheart:OXLDL":           {"loinc": "62307-4", "ucum": "U/L",     "name": "Oxidized LDL"},
}
# fmt: on


# ---------------------------------------------------------------------------
# Synthetic patient fallback. Functional-medicine-typical values for Tyrone.
# ---------------------------------------------------------------------------

SYNTHETIC = {
    "tyrone": {
        "demographics": {
            "id": "tyrone-215-schiller-186",
            "firstName": "Tyrone",
            "lastNameInitial": "S.",
            "dob": "1999-07-12",
            "sex": "male",
            "race": "Black or African American",
            "phenotype_tags": ["athlete", "metabolically-active"],
        },
        "labs": [
            {"loinc": "4548-4",  "name": "HbA1c",           "value": 5.4,  "unit": "%",      "date": "2026-03-14", "source": "Quest"},
            {"loinc": "1554-5",  "name": "Fasting insulin", "value": 7.2,  "unit": "uIU/mL", "date": "2026-03-14", "source": "Quest"},
            {"loinc": "1558-6",  "name": "Fasting glucose", "value": 88,   "unit": "mg/dL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "1884-6",  "name": "ApoB",            "value": 92,   "unit": "mg/dL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "13457-7", "name": "LDL cholesterol", "value": 118,  "unit": "mg/dL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "2085-9",  "name": "HDL cholesterol", "value": 52,   "unit": "mg/dL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "3016-3",  "name": "TSH",             "value": 3.8,  "unit": "mIU/L",  "date": "2026-03-14", "source": "LabCorp"},
            {"loinc": "8099-1",  "name": "TPO antibodies",  "value": 38,   "unit": "IU/mL",  "date": "2026-03-14", "source": "LabCorp"},
            {"loinc": "62292-8", "name": "25-OH Vitamin D", "value": 24,   "unit": "ng/mL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "13965-9", "name": "Homocysteine",    "value": 13.2, "unit": "umol/L", "date": "2026-03-14", "source": "Boston Heart"},
            {"loinc": "2160-0",  "name": "Creatinine",      "value": 1.05, "unit": "mg/dL",  "date": "2026-03-14", "source": "Quest"},
            {"loinc": "30522-7", "name": "hs-CRP",          "value": 1.2,  "unit": "mg/L",   "date": "2026-03-14", "source": "Quest"},
            {"loinc": "2986-8",  "name": "Total testosterone","value": 612,"unit": "ng/dL", "date": "2026-03-14", "source": "Quest"},
        ],
        "medications": [
            {"rxnorm": "6809", "name": "Metformin", "dose": "1000 mg", "frequency": "BID", "start": "2024-09-01"},
        ],
        "genomics": [
            {"rsid": "rs4680",   "gene": "COMT",   "genotype": "Val/Met", "phenotype": "intermediate COMT activity"},
            {"rsid": "rs1801133","gene": "MTHFR",  "genotype": "C/T",     "phenotype": "~35% reduced enzyme activity (heterozygous, C677T)"},
            {"rsid": "rs1801131","gene": "MTHFR",  "genotype": "A/A",     "phenotype": "wild-type A1298C"},
            {"rsid": "rs429358", "gene": "APOE",   "genotype": "e3/e3",   "phenotype": "neutral cardiovascular risk"},
            {"rsid": "rs9939609","gene": "FTO",    "genotype": "A/T",     "phenotype": "heterozygous obesity-risk allele"},
            {"rsid": "rs2228570","gene": "VDR",    "genotype": "F/f",     "phenotype": "intermediate vitamin D receptor activity"},
            {"rsid": "rs1801394","gene": "MTRR",   "genotype": "A/G",     "phenotype": "heterozygous methionine synthase reductase variant"},
            {"rsid": "rs1805087","gene": "MTR",    "genotype": "A/A",     "phenotype": "wild-type methionine synthase"},
            {"rsid": "rs1695",   "gene": "GSTP1",  "genotype": "A/G",     "phenotype": "heterozygous glutathione S-transferase variant"},
        ],
        "wearables": {
            "source": "Oura",  # primary device
            "window": "last_7_days",
            "metrics": {
                "hrv_rmssd_ms_mean": 52.4,
                "rhr_bpm_mean": 56,
                "sleep_score_mean": 81,
                "deep_sleep_min_mean": 78,
                "rem_sleep_min_mean": 92,
                "spo2_pct_min_overnight": 95,
                "body_temp_deviation_c": 0.1,
            },
            "secondary_streams": {
                "Dexcom CGM": {"glucose_mg_dL_mean": 92, "glucose_mg_dL_p95": 134, "time_in_range_70_140_pct": 87},
                "Apple Health": {"steps_per_day_mean": 9820, "active_kcal_per_day_mean": 612},
                "Whoop": {"strain_mean": 11.2, "recovery_pct_mean": 67},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# HTTP client with retries.
# ---------------------------------------------------------------------------

HAPI_BASE = os.environ.get("HAPI_FHIR_BASE", "https://hapi.fhir.org/baseR4")
RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"
HTTP_TIMEOUT = 10.0
MAX_RETRIES = 3


async def http_get_with_retry(url: str, params: dict[str, str] | None = None, request_id: str = "") -> dict[str, Any]:
    """GET with exponential backoff. Retries on 5xx and network errors."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(url, params=params)
                if r.status_code < 500:
                    r.raise_for_status()
                    return r.json()
                last_err = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
            last_err = e
        if attempt < MAX_RETRIES - 1:
            backoff = 0.25 * (2 ** attempt)
            audit(request_id, "http.retry", url=url, attempt=attempt + 1, backoff_s=backoff, err=str(last_err))
            await asyncio.sleep(backoff)
    raise last_err if last_err else RuntimeError("http_get_with_retry failed without error")


async def fhir_get(resource_type: str, params: dict[str, str] | None = None, request_id: str = "") -> dict[str, Any]:
    url = f"{HAPI_BASE}/{resource_type}"
    params = params or {}
    params.setdefault("_format", "json")
    return await http_get_with_retry(url, params, request_id)


# ---------------------------------------------------------------------------
# Tool implementations.
# ---------------------------------------------------------------------------

async def tool_get_patient_demographics(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    pid = (args or {}).get("patient_id", "").strip()
    if not pid or pid.lower().startswith("tyrone"):
        return {"source": "synthetic", "data": SYNTHETIC["tyrone"]["demographics"]}
    try:
        data = await fhir_get(f"Patient/{pid}", request_id=request_id)
        names = data.get("name", [])
        first = (names[0].get("given") or [""])[0] if names else ""
        last = (names[0].get("family") or "")[:1] + "." if names else ""
        return {
            "source": "hapi-fhir-r4",
            "data": {
                "id": data.get("id"),
                "firstName": first,
                "lastNameInitial": last,
                "dob": data.get("birthDate"),
                "sex": data.get("gender"),
            },
        }
    except Exception as e:
        return {"source": "error", "error": str(e), "fallback": SYNTHETIC["tyrone"]["demographics"]}


async def tool_get_patient_labs(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    pid = (args or {}).get("patient_id", "").strip()
    if not pid or pid.lower().startswith("tyrone"):
        return {"source": "synthetic", "patient_id": "tyrone-215", "results": SYNTHETIC["tyrone"]["labs"]}
    try:
        bundle = await fhir_get(
            "Observation",
            {"patient": pid, "category": "laboratory", "_count": "50", "_sort": "-date"},
            request_id=request_id,
        )
        results: list[dict[str, Any]] = []
        for entry in (bundle.get("entry") or []):
            obs = entry.get("resource", {})
            code_block = obs.get("code", {}).get("coding") or []
            loinc = next((c.get("code") for c in code_block if c.get("system") == "http://loinc.org"), None)
            value = obs.get("valueQuantity", {})
            results.append({
                "loinc": loinc,
                "name": obs.get("code", {}).get("text") or (code_block[0].get("display") if code_block else None),
                "value": value.get("value"),
                "unit": value.get("unit") or value.get("code"),
                "date": obs.get("effectiveDateTime"),
                "source": "hapi-fhir-r4",
            })
        return {"source": "hapi-fhir-r4", "patient_id": pid, "count": len(results), "results": results}
    except Exception as e:
        return {"source": "error", "error": str(e)}


async def tool_get_patient_medications(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    pid = (args or {}).get("patient_id", "").strip()
    if not pid or pid.lower().startswith("tyrone"):
        return {"source": "synthetic", "patient_id": "tyrone-215", "medications": SYNTHETIC["tyrone"]["medications"]}
    try:
        bundle = await fhir_get("MedicationStatement", {"patient": pid, "_count": "50"}, request_id=request_id)
        meds: list[dict[str, Any]] = []
        for entry in (bundle.get("entry") or []):
            ms = entry.get("resource", {})
            code_block = (ms.get("medicationCodeableConcept", {}).get("coding") or [])
            rxnorm = next((c.get("code") for c in code_block if "rxnorm" in (c.get("system") or "").lower()), None)
            name = ms.get("medicationCodeableConcept", {}).get("text") or (code_block[0].get("display") if code_block else None)
            meds.append({
                "rxnorm": rxnorm,
                "name": name,
                "dose": (ms.get("dosage") or [{}])[0].get("text"),
                "status": ms.get("status"),
            })
        return {"source": "hapi-fhir-r4", "patient_id": pid, "count": len(meds), "medications": meds}
    except Exception as e:
        return {"source": "error", "error": str(e)}


async def tool_get_patient_genomics(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    pid = (args or {}).get("patient_id", "").strip()
    if not pid or pid.lower().startswith("tyrone"):
        return {"source": "synthetic", "patient_id": "tyrone-215", "variants": SYNTHETIC["tyrone"]["genomics"]}
    return {
        "source": "not-available-from-hapi",
        "note": "HAPI public sandbox rarely carries MolecularSequence/genomics. Wire vendor APIs (23andMe Genotype API, AncestryDNA, NIH ClinVar) for production.",
        "patient_id": pid,
        "variants": [],
    }


async def tool_get_wearable_snapshot(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    pid = (args or {}).get("patient_id", "").strip()
    window = (args or {}).get("window", "last_7_days")
    if not pid or pid.lower().startswith("tyrone"):
        snap = dict(SYNTHETIC["tyrone"]["wearables"])
        snap["window"] = window
        return {"source": "synthetic", "patient_id": "tyrone-215", "snapshot": snap}
    return {
        "source": "wearable-stub",
        "note": "Wire to Oura, Whoop, Apple HealthKit, Withings APIs for live streams.",
        "patient_id": pid,
        "snapshot": {"source": "stub", "window": window, "metrics": {}},
    }


# US <-> SI unit conversion table. Each entry: (from_unit, to_unit) -> factor
# (multiply the from-value by factor to get the to-value).
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    # Glucose
    ("mg/dL", "mmol/L"):  0.0555,
    ("mmol/L", "mg/dL"):  18.0182,
    # Cholesterol (LDL/HDL/total)
    ("mg/dL_chol", "mmol/L"):  0.02586,
    ("mmol/L_chol", "mg/dL"):  38.67,
    # Triglycerides
    ("mg/dL_trig", "mmol/L"):  0.01129,
    ("mmol/L_trig", "mg/dL"):  88.57,
    # Creatinine
    ("mg/dL_cr", "umol/L"):  88.4,
    ("umol/L_cr", "mg/dL"):  0.01131,
    # Vitamin D
    ("ng/mL_d", "nmol/L"):  2.496,
    ("nmol/L_d", "ng/mL"):  0.4006,
    # Vitamin B12
    ("pg/mL_b12", "pmol/L"):  0.738,
    ("pmol/L_b12", "pg/mL"):  1.355,
    # Testosterone
    ("ng/dL_testo", "nmol/L"):  0.0347,
    ("nmol/L_testo", "ng/dL"):  28.84,
    # Hemoglobin
    ("g/dL", "g/L"):  10.0,
    ("g/L", "g/dL"):  0.1,
}


async def tool_convert_units(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Convert between US conventional and SI units. Specify analyte for context-aware conversions.

    analyte options: glucose, cholesterol, triglycerides, creatinine, vitamin_d,
    vitamin_b12, testosterone, hemoglobin.
    """
    a = args or {}
    try:
        value = float(a.get("value"))
    except (TypeError, ValueError):
        return {"error": "value (numeric) required"}
    from_unit = (a.get("from_unit") or "").strip()
    to_unit = (a.get("to_unit") or "").strip()
    analyte = (a.get("analyte") or "").strip().lower()
    if not from_unit or not to_unit:
        return {"error": "from_unit and to_unit required"}

    # For context-dependent analytes (cholesterol/trig/Cr/D/B12/testo), append a suffix
    suffix_map = {
        "cholesterol": "_chol", "ldl": "_chol", "hdl": "_chol",
        "triglycerides": "_trig",
        "creatinine": "_cr",
        "vitamin_d": "_d", "25-oh d": "_d", "vitamin d": "_d",
        "vitamin_b12": "_b12", "b12": "_b12",
        "testosterone": "_testo",
    }
    suffix = suffix_map.get(analyte, "")
    key = (from_unit + suffix, to_unit)
    if key not in UNIT_CONVERSIONS:
        # try reverse direction
        key = (from_unit, to_unit + suffix)
    if key not in UNIT_CONVERSIONS:
        return {
            "error": f"No conversion factor for {from_unit} -> {to_unit} (analyte={analyte or 'unspecified'})",
            "supported": [f"{k[0]} -> {k[1]}" for k in UNIT_CONVERSIONS.keys()],
        }
    factor = UNIT_CONVERSIONS[key]
    converted = round(value * factor, 4)
    return {
        "from": {"value": value, "unit": from_unit},
        "to": {"value": converted, "unit": to_unit},
        "factor": factor,
        "analyte": analyte or None,
    }


async def tool_normalize_panel(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Normalize a raw vendor lab panel to canonical LOINC + UCUM in one batch call.

    Input: {"vendor": "quest", "panel": [{"code": "HBA1C", "value": 5.4, "unit": "%"}, ...]}
    Output: {"normalized": [{"loinc": "...", "name": "...", "value": ..., "unit": "...", "vendor_code": "..."}]}
    """
    a = args or {}
    vendor = (a.get("vendor") or "").strip().lower()
    panel = a.get("panel") or []
    if not vendor or not panel:
        return {"error": "vendor and panel required"}
    normalized: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for item in panel:
        code = str(item.get("code", "")).strip().upper()
        if not code:
            continue
        key = f"{vendor}:{code}"
        mapping = VENDOR_TO_LOINC.get(key)
        if mapping:
            normalized.append({
                "loinc": mapping["loinc"],
                "name": mapping["name"],
                "value": item.get("value"),
                "unit": mapping["ucum"],
                "vendor_code": code,
                "vendor_unit": item.get("unit"),
            })
        else:
            unresolved.append(code)
    return {
        "vendor": vendor,
        "input_count": len(panel),
        "normalized_count": len(normalized),
        "unresolved_count": len(unresolved),
        "normalized": normalized,
        "unresolved_codes": unresolved,
    }


async def tool_fhir_create_medication_statement(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Build (and optionally POST) a FHIR R4 MedicationStatement."""
    a = args or {}
    patient_id = a.get("patient_id", "tyrone-215")
    name = a.get("medication_name")
    rxnorm = a.get("rxnorm")
    dose = a.get("dose")
    frequency = a.get("frequency")
    status = a.get("status", "active")
    do_post = bool(a.get("post", False))
    if not name:
        return {"error": "medication_name required"}

    coding: list[dict[str, str]] = []
    if rxnorm:
        coding.append({"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": str(rxnorm), "display": name})
    resource = {
        "resourceType": "MedicationStatement",
        "status": status,
        "medicationCodeableConcept": {"coding": coding, "text": name},
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": a.get("effective_iso") or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "dosage": [{"text": (f"{dose} {frequency}" if dose and frequency else (dose or frequency or "as directed"))}],
        # informationSource is a Reference in R4; omit it rather than send an invalid display-only object
    }
    result: dict[str, Any] = {"resource": resource, "posted": False}
    if do_post:
        try:
            url = f"{HAPI_BASE}/MedicationStatement"
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(url, json=resource, headers={"Content-Type": "application/fhir+json"})
                r.raise_for_status()
                posted = r.json()
                result["posted"] = True
                result["server_id"] = posted.get("id")
                result["location"] = r.headers.get("Location") or f"{url}/{posted.get('id')}"
        except Exception as e:  # noqa: BLE001
            result["post_error"] = str(e)
    return result


async def tool_fhir_create_condition(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Build (and optionally POST) a FHIR R4 Condition (diagnosis/working impression).

    Use clinical_status="active" for current; "remission"/"resolved" for past.
    verification_status="provisional" for working impressions, "confirmed" only when clinician has confirmed.
    """
    a = args or {}
    patient_id = a.get("patient_id", "tyrone-215")
    text = a.get("text")
    snomed = a.get("snomed_code")
    icd10 = a.get("icd10_code")
    clinical_status = a.get("clinical_status", "active")
    verification_status = a.get("verification_status", "provisional")
    do_post = bool(a.get("post", False))
    if not text:
        return {"error": "text (free-text diagnosis name) required"}

    code_block: list[dict[str, str]] = []
    if snomed:
        code_block.append({"system": "http://snomed.info/sct", "code": str(snomed), "display": text})
    if icd10:
        code_block.append({"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": str(icd10), "display": text})
    resource = {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": clinical_status}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": verification_status}]},
        "code": {"coding": code_block, "text": text},
        "subject": {"reference": f"Patient/{patient_id}"},
        "recordedDate": a.get("recorded_iso") or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        # asserter is a Reference in R4; omit it rather than send invalid shape
    }
    result: dict[str, Any] = {"resource": resource, "posted": False}
    if do_post:
        try:
            url = f"{HAPI_BASE}/Condition"
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(url, json=resource, headers={"Content-Type": "application/fhir+json"})
                r.raise_for_status()
                posted = r.json()
                result["posted"] = True
                result["server_id"] = posted.get("id")
                result["location"] = r.headers.get("Location") or f"{url}/{posted.get('id')}"
        except Exception as e:  # noqa: BLE001
            result["post_error"] = str(e)
    return result


async def tool_normalize_biomarker(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    vendor = (args or {}).get("vendor", "").strip().lower()
    code = (args or {}).get("vendor_code", "").strip().upper()
    if not vendor or not code:
        return {"error": "vendor and vendor_code are required"}
    key = f"{vendor}:{code}"
    if key in VENDOR_TO_LOINC:
        return {"vendor_input": key, "canonical": VENDOR_TO_LOINC[key]}
    return {"vendor_input": key, "canonical": None, "note": "Not in static map. Extend VENDOR_TO_LOINC or call a terminology service like UMLS or Athena."}


async def tool_fhir_passthrough(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    resource = (args or {}).get("resource_type", "Patient")
    params = (args or {}).get("params", {})
    try:
        return await fhir_get(resource, params, request_id=request_id)
    except Exception as e:
        return {"error": str(e)}


# ---- Clinical calculators ---------------------------------------------------

async def tool_calc_homa_ir(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """HOMA-IR = (fasting insulin (uIU/mL) * fasting glucose (mg/dL)) / 405."""
    try:
        insulin = float(args.get("fasting_insulin_uIU_mL"))
        glucose = float(args.get("fasting_glucose_mg_dL"))
    except (TypeError, ValueError, KeyError):
        return {"error": "fasting_insulin_uIU_mL and fasting_glucose_mg_dL are required (numeric)"}
    homa = round((insulin * glucose) / 405.0, 2)
    if homa < 1.0:
        interp = "Optimal insulin sensitivity"
    elif homa < 2.0:
        interp = "Within reference range"
    elif homa < 2.75:
        interp = "Early insulin resistance"
    else:
        interp = "Insulin resistance"
    return {
        "homa_ir": homa,
        "interpretation": interp,
        "formula": "(fasting_insulin_uIU_mL * fasting_glucose_mg_dL) / 405",
        "inputs": {"insulin": insulin, "glucose": glucose},
        "reference": "Matthews et al, Diabetologia 1985",
    }


async def tool_calc_egfr_ckdepi_2021(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """CKD-EPI 2021 race-free creatinine equation."""
    try:
        scr = float(args.get("creatinine_mg_dL"))
        age = float(args.get("age_years"))
        sex = (args.get("sex") or "").strip().lower()
        if sex not in ("male", "female"):
            return {"error": "sex must be 'male' or 'female'"}
    except (TypeError, ValueError, KeyError):
        return {"error": "creatinine_mg_dL (numeric), age_years (numeric), sex ('male'|'female') are required"}
    if sex == "female":
        kappa, alpha, sex_mult = 0.7, -0.241, 1.012
    else:
        kappa, alpha, sex_mult = 0.9, -0.302, 1.0
    egfr = 142 * min(scr / kappa, 1) ** alpha * max(scr / kappa, 1) ** -1.200 * 0.9938 ** age * sex_mult
    egfr = round(egfr, 1)
    if egfr >= 90:
        stage = "G1 - normal/high"
    elif egfr >= 60:
        stage = "G2 - mildly decreased"
    elif egfr >= 45:
        stage = "G3a - mild-moderate decrease"
    elif egfr >= 30:
        stage = "G3b - moderate-severe decrease"
    elif egfr >= 15:
        stage = "G4 - severe decrease"
    else:
        stage = "G5 - kidney failure"
    return {
        "egfr_mL_min_1_73m2": egfr,
        "ckd_stage": stage,
        "inputs": {"creatinine": scr, "age": age, "sex": sex},
        "reference": "Inker LA et al, NEJM 2021 (CKD-EPI 2021, race-free)",
    }


async def tool_calc_ascvd_10yr(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """ACC/AHA 2013 Pooled Cohort 10-year ASCVD risk."""
    try:
        age = float(args.get("age_years"))
        sex = (args.get("sex") or "").lower()
        race = (args.get("race") or "white").lower()
        tc = float(args.get("total_cholesterol_mg_dL"))
        hdl = float(args.get("hdl_mg_dL"))
        sbp = float(args.get("sbp_mmHg"))
        treated_bp = bool(args.get("treated_for_hypertension", False))
        diabetes = bool(args.get("diabetes", False))
        smoker = bool(args.get("smoker", False))
    except (TypeError, ValueError, KeyError):
        return {"error": "age_years, sex, total_cholesterol_mg_dL, hdl_mg_dL, sbp_mmHg, treated_for_hypertension, diabetes, smoker all required"}

    is_aa = race in ("african american", "black", "aa")
    if sex == "female":
        if is_aa:
            coefs = {"ln_age": 17.114, "ln_age_sq": 0, "ln_tc": 0.94, "ln_age_ln_tc": 0,
                     "ln_hdl": -18.92, "ln_age_ln_hdl": 4.475,
                     "ln_treated_sbp": 29.291, "ln_age_ln_treated_sbp": -6.432,
                     "ln_untreated_sbp": 27.82, "ln_age_ln_untreated_sbp": -6.087,
                     "smoker": 0.691, "ln_age_smoker": 0, "diabetes": 0.874, "baseline": 0.9533, "mean": 86.61}
        else:
            coefs = {"ln_age": -29.799, "ln_age_sq": 4.884, "ln_tc": 13.54, "ln_age_ln_tc": -3.114,
                     "ln_hdl": -13.578, "ln_age_ln_hdl": 3.149,
                     "ln_treated_sbp": 2.019, "ln_age_ln_treated_sbp": 0,
                     "ln_untreated_sbp": 1.957, "ln_age_ln_untreated_sbp": 0,
                     "smoker": 7.574, "ln_age_smoker": -1.665, "diabetes": 0.661, "baseline": 0.9665, "mean": -29.18}
    else:
        if is_aa:
            coefs = {"ln_age": 2.469, "ln_age_sq": 0, "ln_tc": 0.302, "ln_age_ln_tc": 0,
                     "ln_hdl": -0.307, "ln_age_ln_hdl": 0,
                     "ln_treated_sbp": 1.916, "ln_age_ln_treated_sbp": 0,
                     "ln_untreated_sbp": 1.809, "ln_age_ln_untreated_sbp": 0,
                     "smoker": 0.549, "ln_age_smoker": 0, "diabetes": 0.645, "baseline": 0.8954, "mean": 19.54}
        else:
            coefs = {"ln_age": 12.344, "ln_age_sq": 0, "ln_tc": 11.853, "ln_age_ln_tc": -2.664,
                     "ln_hdl": -7.99, "ln_age_ln_hdl": 1.769,
                     "ln_treated_sbp": 1.797, "ln_age_ln_treated_sbp": 0,
                     "ln_untreated_sbp": 1.764, "ln_age_ln_untreated_sbp": 0,
                     "smoker": 7.837, "ln_age_smoker": -1.795, "diabetes": 0.658, "baseline": 0.9144, "mean": 61.18}

    ln_age = math.log(age)
    ln_tc = math.log(tc)
    ln_hdl = math.log(hdl)
    ln_sbp = math.log(sbp)
    s = (
        coefs["ln_age"] * ln_age
        + coefs["ln_age_sq"] * ln_age * ln_age
        + coefs["ln_tc"] * ln_tc
        + coefs["ln_age_ln_tc"] * ln_age * ln_tc
        + coefs["ln_hdl"] * ln_hdl
        + coefs["ln_age_ln_hdl"] * ln_age * ln_hdl
        + (coefs["ln_treated_sbp"] * ln_sbp + coefs["ln_age_ln_treated_sbp"] * ln_age * ln_sbp) * (1 if treated_bp else 0)
        + (coefs["ln_untreated_sbp"] * ln_sbp + coefs["ln_age_ln_untreated_sbp"] * ln_age * ln_sbp) * (0 if treated_bp else 1)
        + coefs["smoker"] * (1 if smoker else 0)
        + coefs["ln_age_smoker"] * ln_age * (1 if smoker else 0)
        + coefs["diabetes"] * (1 if diabetes else 0)
    )
    risk = 1 - coefs["baseline"] ** math.exp(s - coefs["mean"])
    risk_pct = round(risk * 100, 1)
    if risk_pct < 5:
        cat = "Low (<5%)"
    elif risk_pct < 7.5:
        cat = "Borderline (5 to 7.5%)"
    elif risk_pct < 20:
        cat = "Intermediate (7.5 to 20%)"
    else:
        cat = "High (>=20%)"
    return {
        "ascvd_10yr_risk_pct": risk_pct,
        "category": cat,
        "inputs": args,
        "reference": "Goff DC et al, ACC/AHA 2013 Pooled Cohort Equations",
        "note": "Validated for ages 40-79. Outside that range treat as exploratory.",
    }


# Functional-medicine optimal ranges (narrower than standard reference ranges).
# Used by calc_reference_ranges. Each entry: LOINC -> {ref_low, ref_high, opt_low,
# opt_high, unit, citation}. These are narrative defaults; production should
# pull from CLIA-lab-derived per-assay ranges.
OPTIMAL_RANGES: dict[str, dict[str, Any]] = {
    "4548-4":  {"ref_low": 4.0, "ref_high": 5.6, "opt_low": 4.5, "opt_high": 5.2, "unit": "%", "marker": "HbA1c", "cite": "ADA 2024 + functional medicine"},
    "1558-6":  {"ref_low": 70, "ref_high": 99, "opt_low": 75, "opt_high": 88, "unit": "mg/dL", "marker": "Fasting glucose", "cite": "ADA + Bredesen"},
    "1554-5":  {"ref_low": 2.6, "ref_high": 24.9, "opt_low": 3, "opt_high": 7, "unit": "uIU/mL", "marker": "Fasting insulin", "cite": "IFM optimal"},
    "1884-6":  {"ref_low": 0, "ref_high": 100, "opt_low": 0, "opt_high": 80, "unit": "mg/dL", "marker": "ApoB", "cite": "NLA 2024"},
    "13457-7": {"ref_low": 0, "ref_high": 100, "opt_low": 0, "opt_high": 100, "unit": "mg/dL", "marker": "LDL cholesterol", "cite": "ACC/AHA 2018"},
    "2085-9":  {"ref_low": 40, "ref_high": 999, "opt_low": 50, "opt_high": 999, "unit": "mg/dL", "marker": "HDL cholesterol", "cite": "ACC/AHA"},
    "2571-8":  {"ref_low": 0, "ref_high": 150, "opt_low": 0, "opt_high": 100, "unit": "mg/dL", "marker": "Triglycerides", "cite": "ATP III + IFM"},
    "10835-7": {"ref_low": 0, "ref_high": 75, "opt_low": 0, "opt_high": 30, "unit": "nmol/L", "marker": "Lipoprotein(a)", "cite": "NLA 2023"},
    "3016-3":  {"ref_low": 0.5, "ref_high": 4.5, "opt_low": 1.0, "opt_high": 2.0, "unit": "mIU/L", "marker": "TSH", "cite": "AACE + functional medicine"},
    "3051-0":  {"ref_low": 2.0, "ref_high": 4.4, "opt_low": 3.2, "opt_high": 4.4, "unit": "pg/mL", "marker": "Free T3", "cite": "AACE"},
    "3024-7":  {"ref_low": 0.8, "ref_high": 1.8, "opt_low": 1.2, "opt_high": 1.6, "unit": "ng/dL", "marker": "Free T4", "cite": "AACE"},
    "8099-1":  {"ref_low": 0, "ref_high": 35, "opt_low": 0, "opt_high": 9, "unit": "IU/mL", "marker": "TPO antibodies", "cite": "AACE Hashimoto"},
    "62292-8": {"ref_low": 30, "ref_high": 100, "opt_low": 50, "opt_high": 80, "unit": "ng/mL", "marker": "25-OH Vitamin D", "cite": "Holick / Endocrine Society"},
    "2132-9":  {"ref_low": 200, "ref_high": 1100, "opt_low": 500, "opt_high": 1100, "unit": "pg/mL", "marker": "Vitamin B12", "cite": "B12 deficiency syndromes"},
    "13965-9": {"ref_low": 0, "ref_high": 15, "opt_low": 0, "opt_high": 7, "unit": "umol/L", "marker": "Homocysteine", "cite": "AHA + functional medicine"},
    "30522-7": {"ref_low": 0, "ref_high": 3.0, "opt_low": 0, "opt_high": 1.0, "unit": "mg/L", "marker": "hs-CRP", "cite": "AHA 2022"},
    "2276-4":  {"ref_low": 30, "ref_high": 300, "opt_low": 50, "opt_high": 150, "unit": "ng/mL", "marker": "Ferritin", "cite": "Functional medicine"},
    "2986-8":  {"ref_low": 264, "ref_high": 916, "opt_low": 500, "opt_high": 800, "unit": "ng/dL", "marker": "Total testosterone", "cite": "Endocrine Society 2018"},
    "2160-0":  {"ref_low": 0.7, "ref_high": 1.3, "opt_low": 0.7, "opt_high": 1.0, "unit": "mg/dL", "marker": "Creatinine", "cite": "Standard"},
}


async def tool_interpret_vitals(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Interpret a vital signs panel. Detects critical values.

    Inputs (any subset): systolic_bp_mmHg, diastolic_bp_mmHg, heart_rate_bpm,
    respiratory_rate_per_min, temperature_c (or temperature_f), spo2_pct.
    """
    a = args or {}
    findings: list[dict[str, Any]] = []
    criticals: list[str] = []

    def add(name: str, value: float, unit: str, low_ok: float, high_ok: float,
            crit_low: float | None = None, crit_high: float | None = None) -> None:
        f = {"name": name, "value": value, "unit": unit, "in_range": low_ok <= value <= high_ok}
        if crit_low is not None and value < crit_low:
            f["critical"] = True
            criticals.append(f"{name} {value} {unit} below panic threshold {crit_low}")
        elif crit_high is not None and value > crit_high:
            f["critical"] = True
            criticals.append(f"{name} {value} {unit} above panic threshold {crit_high}")
        else:
            f["critical"] = False
        findings.append(f)

    sbp = a.get("systolic_bp_mmHg")
    dbp = a.get("diastolic_bp_mmHg")
    if sbp is not None:
        sbp = float(sbp)
        add("Systolic BP", sbp, "mmHg", 90, 130, crit_low=80, crit_high=180)
    if dbp is not None:
        dbp = float(dbp)
        add("Diastolic BP", dbp, "mmHg", 60, 80, crit_low=50, crit_high=120)
    hr = a.get("heart_rate_bpm")
    if hr is not None:
        add("Heart rate", float(hr), "bpm", 60, 100, crit_low=40, crit_high=130)
    rr = a.get("respiratory_rate_per_min")
    if rr is not None:
        add("Respiratory rate", float(rr), "/min", 12, 20, crit_low=8, crit_high=30)
    spo2 = a.get("spo2_pct")
    if spo2 is not None:
        add("SpO2", float(spo2), "%", 95, 100, crit_low=88)
    t_c = a.get("temperature_c")
    t_f = a.get("temperature_f")
    if t_c is None and t_f is not None:
        t_c = (float(t_f) - 32) * 5 / 9
    if t_c is not None:
        add("Temperature", float(t_c), "C", 36.0, 37.5, crit_low=35.0, crit_high=39.0)

    # MAP if both BP present
    extras: dict[str, Any] = {}
    if sbp is not None and dbp is not None:
        map_val = round((sbp + 2 * dbp) / 3, 1)
        extras["mean_arterial_pressure"] = {"value": map_val, "unit": "mmHg", "in_range": 70 <= map_val <= 100}

    return {
        "findings": findings,
        "critical_values": criticals,
        "has_critical": bool(criticals),
        "extras": extras,
        "headline": ("Critical value flagged for same-day clinician contact" if criticals
                     else "Vitals within reference range" if all(f["in_range"] for f in findings)
                     else "Vitals show non-critical deviations"),
    }


async def tool_fhir_create_observation(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Build a FHIR R4 Observation from a single normalized lab value. POSTs when post=true."""
    a = args or {}
    loinc = a.get("loinc")
    value = a.get("value")
    unit = a.get("unit", "")
    patient_id = a.get("patient_id", "tyrone-215")
    do_post = bool(a.get("post", False))
    effective = a.get("effective_iso") or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

    if not loinc or value is None:
        return {"error": "loinc and value required"}
    try:
        v = float(value)
    except (TypeError, ValueError):
        return {"error": "value must be numeric"}

    # Find display from the static OPTIMAL_RANGES if we can
    display = (OPTIMAL_RANGES.get(loinc) or {}).get("marker") or a.get("display") or "Lab Observation"

    resource = {
        "resourceType": "Observation",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory",
                "display": "Laboratory",
            }],
        }],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": loinc, "display": display}],
            "text": display,
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "effectiveDateTime": effective,
        "valueQuantity": {"value": v, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
        "performer": [{"display": "Longevity Copilot MCP v1.3"}],
    }

    result: dict[str, Any] = {"resource": resource, "posted": False}
    if do_post:
        try:
            url = f"{HAPI_BASE}/Observation"
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(url, json=resource, headers={"Content-Type": "application/fhir+json"})
                r.raise_for_status()
                posted = r.json()
                result["posted"] = True
                result["server_id"] = posted.get("id")
                result["location"] = r.headers.get("Location") or f"{url}/{posted.get('id')}"
        except Exception as e:  # noqa: BLE001
            result["post_error"] = str(e)
    else:
        result["note"] = "post=false. Set post=true to actually create the resource."
    return result


TOOL_LAYERS: dict[str, str] = {
    # ---- data plane: read, normalize, standardize ----
    "get_patient_demographics":  "data-plane",
    "get_patient_labs":          "data-plane",
    "get_patient_medications":   "data-plane",
    "get_patient_genomics":      "data-plane",
    "get_wearable_snapshot":     "data-plane",
    "normalize_biomarker":       "data-plane",
    "normalize_panel":           "data-plane",
    "normalize_wearable_metric": "data-plane",
    "convert_units":             "data-plane",
    "calc_reference_ranges":     "data-plane",
    "fhir_passthrough":          "data-plane",
    "list_supported_sources":    "data-plane",
    "simulate_lab_panel":        "data-plane",
    "rxnav_interactions":        "data-plane",
    # ---- agent-side helpers: reasoning support that agents call ----
    "calc_homa_ir":              "agent-helper",
    "calc_egfr_ckdepi_2021":     "agent-helper",
    "calc_ascvd_10yr":           "agent-helper",
    "calc_fib4":                 "agent-helper",
    "calc_findrisc":             "agent-helper",
    "calc_bmi_bsa":              "agent-helper",
    "interpret_vitals":          "agent-helper",
    "drug_interaction_matrix":   "agent-helper",
    "chart_lab_trend":           "agent-helper",
    "generate_clinical_pdf":     "agent-helper",
    "generate_patient_education_pdf": "agent-helper",
    "fhir_create_observation":   "agent-helper",
    "fhir_create_medication_statement": "agent-helper",
    "fhir_create_condition":     "agent-helper",
    "fhir_create_diagnostic_report": "agent-helper",
    # ---- observability ----
    "list_reports":              "ops",
    "audit_tail":                "ops",
    "discover":                  "ops",
}


async def tool_discover(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Return the full system manifest in one call: tool counts, coverage, sample requests, endpoints."""
    sample_requests = {
        "get_patient_labs": {"patient_id": "tyrone"},
        "normalize_biomarker": {"vendor": "labcorp", "vendor_code": "HEMOGLOBIN_A1C"},
        "calc_homa_ir": {"fasting_insulin_uIU_mL": 7.2, "fasting_glucose_mg_dL": 88},
        "calc_egfr_ckdepi_2021": {"creatinine_mg_dL": 1.05, "age_years": 26, "sex": "male"},
        "calc_ascvd_10yr": {"age_years": 55, "sex": "male", "race": "white", "total_cholesterol_mg_dL": 213, "hdl_mg_dL": 50, "sbp_mmHg": 120, "treated_for_hypertension": False, "diabetes": False, "smoker": False},
        "calc_reference_ranges": {"loinc": "3016-3", "value": 3.8},
        "chart_lab_trend": {"marker": "HbA1c", "unit": "%", "loinc": "4548-4", "series": [{"date": "2025-03-14", "value": 6.1}, {"date": "2026-03-14", "value": 5.4}]},
        "generate_clinical_pdf": {"headline": "Brief", "findings": [{"name": "HbA1c", "value": 5.4, "unit": "%"}]},
    }
    return {
        "server": {"name": "longevity-copilot-mcp", "version": "1.3.0"},
        "tool_count": len(TOOLS),
        "tool_names": [t["name"] for t in TOOLS],
        "endpoints": {
            "mcp": "/mcp",
            "catalog": "/catalog",
            "dashboard": "/dashboard",
            "scorecard": "/scorecard/{patient_id}",
            "reports": "/reports/{id}",
            "charts": "/charts/{id}",
            "metrics": "/metrics",
            "agent_card": "/.well-known/agent.json",
            "openapi": "/openapi.json",
        },
        "coverage": {
            "lab_vendors": len(LAB_VENDORS),
            "wearables": len(WEARABLE_SOURCES),
            "ehr_sources": len(EHR_SOURCES),
            "biomarkers": len({v["name"] for v in VENDOR_TO_LOINC.values()}),
            "vendor_mappings": len(VENDOR_TO_LOINC),
            "calculators": sum(1 for t in TOOLS if t["name"].startswith("calc_")),
        },
        "sample_requests": sample_requests,
    }


async def tool_simulate_lab_panel(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Generate a plausible synthetic lab panel from a patient profile. For demos and stress testing."""
    a = args or {}
    age = int(a.get("age_years", 35))
    sex = (a.get("sex") or "male").lower()
    profile = (a.get("profile") or "healthy").lower()
    import random
    rng = random.Random(a.get("seed"))

    # Base healthy ranges with mild jitter
    def jitter(mid, span):
        return round(mid + rng.uniform(-span, span), 2)

    panel = [
        {"loinc": "4548-4",  "name": "HbA1c",           "value": jitter(5.2, 0.2),  "unit": "%"},
        {"loinc": "1558-6",  "name": "Fasting glucose", "value": jitter(88, 6),     "unit": "mg/dL"},
        {"loinc": "1554-5",  "name": "Fasting insulin", "value": jitter(6, 2),      "unit": "uIU/mL"},
        {"loinc": "1884-6",  "name": "ApoB",            "value": jitter(85, 12),    "unit": "mg/dL"},
        {"loinc": "13457-7", "name": "LDL cholesterol", "value": jitter(110, 18),   "unit": "mg/dL"},
        {"loinc": "2085-9",  "name": "HDL cholesterol", "value": jitter(55, 8),     "unit": "mg/dL"},
        {"loinc": "3016-3",  "name": "TSH",             "value": jitter(2.0, 0.6),  "unit": "mIU/L"},
        {"loinc": "62292-8", "name": "25-OH Vitamin D", "value": jitter(38, 8),     "unit": "ng/mL"},
        {"loinc": "13965-9", "name": "Homocysteine",    "value": jitter(9, 2),      "unit": "umol/L"},
        {"loinc": "30522-7", "name": "hs-CRP",          "value": jitter(0.8, 0.4),  "unit": "mg/L"},
    ]
    # Apply profile shifts
    if profile == "metabolic_syndrome":
        for p in panel:
            if p["loinc"] == "4548-4": p["value"] = jitter(5.9, 0.2)
            if p["loinc"] == "1554-5": p["value"] = jitter(18, 3)
            if p["loinc"] == "2571-8" or p["name"] == "Triglycerides": p["value"] = jitter(180, 30)
            if p["loinc"] == "1884-6": p["value"] = jitter(115, 10)
    elif profile == "hashimoto":
        for p in panel:
            if p["loinc"] == "3016-3": p["value"] = jitter(4.5, 0.5)
        panel.append({"loinc": "8099-1", "name": "TPO antibodies", "value": jitter(150, 50), "unit": "IU/mL"})
    elif profile == "insulin_resistant":
        for p in panel:
            if p["loinc"] == "1554-5": p["value"] = jitter(15, 3)
            if p["loinc"] == "1558-6": p["value"] = jitter(100, 6)
    return {
        "patient_profile": {"age_years": age, "sex": sex, "profile": profile},
        "panel": panel,
        "note": "Synthetic data only - generated by simulate_lab_panel. Not from a real patient.",
    }


async def tool_calc_reference_ranges(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Given a LOINC + optional value, return standard reference range, functional-medicine optimal range, and verdict."""
    loinc = (args or {}).get("loinc", "").strip()
    value = (args or {}).get("value")
    if not loinc:
        return {"error": "loinc required"}
    rng = OPTIMAL_RANGES.get(loinc)
    if not rng:
        return {"loinc": loinc, "found": False, "note": f"No optimal range on file for LOINC {loinc}. Extend OPTIMAL_RANGES."}
    result = {
        "loinc": loinc,
        "marker": rng["marker"],
        "unit": rng["unit"],
        "reference_range": {"low": rng["ref_low"], "high": rng["ref_high"]},
        "optimal_range": {"low": rng["opt_low"], "high": rng["opt_high"]},
        "citation": rng["cite"],
    }
    if value is not None:
        try:
            v = float(value)
            in_ref = rng["ref_low"] <= v <= rng["ref_high"]
            in_opt = rng["opt_low"] <= v <= rng["opt_high"]
            if in_opt:
                verdict = "Within functional-medicine optimal range"
            elif in_ref:
                verdict = "Within standard reference range, outside optimal"
            else:
                verdict = "Outside standard reference range - clinical attention"
            result["value"] = v
            result["verdict"] = verdict
            result["in_reference"] = in_ref
            result["in_optimal"] = in_opt
        except (TypeError, ValueError):
            result["value_error"] = f"Could not parse value: {value}"
    return result


# Generated trend charts persisted to disk and served via GET /charts/{id}.
CHARTS_DIR = Path(os.environ.get("CHARTS_DIR", "/tmp/lc-charts"))
CHARTS_DIR.mkdir(parents=True, exist_ok=True)
CHARTS_INDEX: dict[str, dict[str, Any]] = {}


async def tool_chart_lab_trend(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Render a PNG line chart of a lab marker over time.

    Args: {"marker": "HbA1c", "unit": "%", "loinc": "4548-4" (optional),
           "series": [{"date": "2024-01-15", "value": 5.8}, ...]}
    Returns: {chart_id, url, markdown_link} - agent drops the link in chat.
    """
    if not MATPLOTLIB_OK:
        return {"error": "matplotlib not installed; add it to requirements.txt"}
    marker = (args or {}).get("marker", "Marker")
    unit = (args or {}).get("unit", "")
    loinc = (args or {}).get("loinc")
    series = (args or {}).get("series") or []
    if not series:
        return {"error": "series required (array of {date, value})"}
    try:
        from datetime import datetime
        pts = []
        for p in series:
            d = datetime.fromisoformat(str(p["date"]))
            v = float(p["value"])
            pts.append((d, v))
        pts.sort()
        if not pts:
            return {"error": "no valid points after parsing"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"parse failed: {e}"}

    chart_id = f"chart-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    out = CHARTS_DIR / f"{chart_id}.png"

    fig, ax = plt.subplots(figsize=(8, 4), dpi=120, facecolor="#F7F3EC")
    ax.set_facecolor("#FBFAF6")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ax.plot(xs, ys, marker="o", color="#1F3A2E", linewidth=2)

    # Overlay optimal range if available
    if loinc and loinc in OPTIMAL_RANGES:
        r = OPTIMAL_RANGES[loinc]
        ax.axhspan(r["opt_low"], r["opt_high"], alpha=0.12, color="#C8D2C2", label="Optimal range")
        ax.axhspan(r["ref_low"], r["ref_high"], alpha=0.05, color="#1F3A2E", label="Reference range")
        ax.legend(loc="upper right", frameon=False, fontsize=9)

    title = f"{marker} trend" + (f" ({unit})" if unit else "")
    ax.set_title(title, fontsize=14, color="#1F3A2E", loc="left", pad=10)
    ax.set_xlabel("Date", color="#6B6760", fontsize=10)
    ax.set_ylabel(unit or "value", color="#6B6760", fontsize=10)
    ax.tick_params(colors="#6B6760", labelsize=9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#ECE6DA")
    ax.spines["bottom"].set_color("#ECE6DA")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)

    size = out.stat().st_size
    rel = f"/charts/{chart_id}"
    full = f"{PUBLIC_BASE_URL}{rel}" if PUBLIC_BASE_URL else rel
    CHARTS_INDEX[chart_id] = {
        "id": chart_id, "path": str(out), "bytes": size,
        "created_ts": int(time.time()), "marker": marker, "point_count": len(pts),
    }
    return {
        "chart_id": chart_id, "url": full, "relative_url": rel, "bytes": size,
        "markdown_link": f"![{marker} trend]({full})",
        "point_count": len(pts),
    }


async def tool_drug_interaction_matrix(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Build an NxN interaction matrix for a list of drugs/supplements.

    For each pair, look up both drugs in RxNav (best-effort) and return a matrix
    entry. The matrix entry includes a coarse interaction class. Actual clinical
    interaction reasoning belongs in the Drug-Supplement Check specialist; this
    tool is the structured grid the specialist consumes.
    """
    drugs = (args or {}).get("drugs") or []
    if not isinstance(drugs, list) or len(drugs) < 2:
        return {"error": "drugs must be a list of at least 2 names"}

    # Resolve each to RxCUI (best-effort)
    resolutions = {}
    for d in drugs:
        name = str(d).strip()
        try:
            url = f"{RXNAV_BASE}/rxcui.json"
            data = await http_get_with_retry(url, {"name": name, "search": "2"}, request_id=request_id)
            ids = (data.get("idGroup") or {}).get("rxnormId") or []
            resolutions[name] = {"rxcui": ids[0] if ids else None}
        except Exception as e:  # noqa: BLE001
            resolutions[name] = {"rxcui": None, "error": str(e)}

    # Hard-coded known dangerous / notable pairs (extend with a real interaction DB).
    KNOWN_PAIRS = {
        frozenset({"metformin", "berberine"}): {"severity": "moderate", "note": "Additive AMPK + glucose-lowering. Hypoglycemia risk if also on insulin/SU. Confirm eGFR >=45 (OCT2)."},
        frozenset({"metformin", "alcohol"}):   {"severity": "high",     "note": "Lactic acidosis risk with heavy alcohol use."},
        frozenset({"warfarin", "vitamin k"}):  {"severity": "high",     "note": "Vitamin K antagonism reduces warfarin effect."},
        frozenset({"ssri", "tramadol"}):       {"severity": "high",     "note": "Serotonin syndrome risk."},
        frozenset({"statin", "grapefruit"}):   {"severity": "moderate", "note": "CYP3A4 inhibition - raises simvastatin/atorvastatin levels."},
        frozenset({"levothyroxine", "calcium"}): {"severity": "moderate", "note": "Calcium chelates levothyroxine. Separate doses by 4 hours."},
        frozenset({"levothyroxine", "iron"}):  {"severity": "moderate", "note": "Iron chelates levothyroxine. Separate doses by 4 hours."},
    }

    n = len(drugs)
    matrix: list[list[dict[str, Any]]] = []
    for i, a in enumerate(drugs):
        row = []
        for j, b in enumerate(drugs):
            if i == j:
                row.append({"severity": "self"})
                continue
            pair = frozenset({str(a).strip().lower(), str(b).strip().lower()})
            hit = KNOWN_PAIRS.get(pair)
            if hit:
                row.append(hit)
            else:
                row.append({"severity": "unknown", "note": "No entry in static KB. Route to Drug-Supplement Check for reasoning."})
        matrix.append(row)
    return {
        "drugs": list(drugs),
        "resolutions": resolutions,
        "matrix": matrix,
        "note": "Static KB demo. Production: wire to Lexicomp, Micromedex, or DrugBank.",
    }


async def tool_fhir_create_diagnostic_report(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Construct a FHIR R4 DiagnosticReport from a clinical brief.

    Optionally POSTs it to the configured FHIR base when post=true. Returns the
    resource (and the server-assigned id if it was posted). This closes the
    read + write loop: the agent can now write findings back to the EHR, not
    just read from it.
    """
    brief = args or {}
    patient_id = brief.get("patient_id") or "tyrone-215"
    headline = brief.get("headline") or "Longevity Copilot clinical brief"
    findings = brief.get("findings") or []
    plan = brief.get("plan") or []
    monitoring = brief.get("monitoring") or []
    do_post = bool(brief.get("post", False))

    if isinstance(plan, list):
        plan_text = "\n".join(f"- {p}" for p in plan)
    else:
        plan_text = str(plan)
    if isinstance(monitoring, list):
        mon_text = "\n".join(f"- {m}" for m in monitoring)
    else:
        mon_text = str(monitoring)

    conclusion_lines = [headline]
    if brief.get("clinical_reasoning"):
        conclusion_lines.append("")
        conclusion_lines.append(str(brief["clinical_reasoning"]))
    if plan_text:
        conclusion_lines.append("")
        conclusion_lines.append("Plan:")
        conclusion_lines.append(plan_text)
    if mon_text:
        conclusion_lines.append("")
        conclusion_lines.append("Monitoring:")
        conclusion_lines.append(mon_text)
    conclusion_lines.append("")
    conclusion_lines.append(brief.get("scope_statement",
        "For licensed-clinician review. Not for direct patient distribution without clinician sign-off."))
    conclusion = "\n".join(conclusion_lines)

    issued = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    resource = {
        "resourceType": "DiagnosticReport",
        "status": "preliminary",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "OTH",
                "display": "Other / longevity workup",
            }],
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "11506-3",
                "display": "Progress note",
            }],
            "text": "Longevity Copilot clinical brief",
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "issued": issued,
        "performer": [{"display": "Longevity Copilot MCP v1.2"}],
        "result": [
            {
                "display": f"{f.get('name','')} {f.get('value','')} {f.get('unit','')}".strip(),
            }
            for f in findings
        ],
        "conclusion": conclusion,
        "extension": [
            {
                "url": "https://longevitycopilot.example/extensions/audit-trail",
                "valueString": f"Generated by Longevity Copilot MCP, request_id={request_id}",
            },
        ],
    }

    result: dict[str, Any] = {"resource": resource}
    if do_post:
        try:
            url = f"{HAPI_BASE}/DiagnosticReport"
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(url, json=resource, headers={"Content-Type": "application/fhir+json"})
                r.raise_for_status()
                posted = r.json()
                result["posted"] = True
                result["server_id"] = posted.get("id")
                result["location"] = r.headers.get("Location") or f"{url}/{posted.get('id')}"
        except Exception as e:  # noqa: BLE001
            result["posted"] = False
            result["post_error"] = str(e)
    else:
        result["posted"] = False
        result["note"] = "post=false. Set post=true to actually create the resource on the configured FHIR server."
    return result


async def tool_calc_fib4(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """FIB-4 score for liver fibrosis. FIB-4 = (age * AST) / (platelets * sqrt(ALT))."""
    try:
        age = float(args.get("age_years"))
        ast = float(args.get("ast_U_L"))
        alt = float(args.get("alt_U_L"))
        platelets = float(args.get("platelets_10e9_L"))
    except (TypeError, ValueError, KeyError):
        return {"error": "age_years, ast_U_L, alt_U_L, platelets_10e9_L all required (numeric)"}
    if alt <= 0 or platelets <= 0:
        return {"error": "alt_U_L and platelets_10e9_L must be positive"}
    fib4 = (age * ast) / (platelets * math.sqrt(alt))
    fib4 = round(fib4, 2)
    if age < 65:
        if fib4 < 1.30: interp = "Low risk for advanced fibrosis"
        elif fib4 <= 2.67: interp = "Indeterminate - consider transient elastography"
        else: interp = "High risk - hepatology referral"
    else:
        if fib4 < 2.0: interp = "Low risk (age-adjusted cutoff)"
        elif fib4 <= 2.67: interp = "Indeterminate"
        else: interp = "High risk - hepatology referral"
    return {
        "fib4": fib4,
        "interpretation": interp,
        "formula": "(age * AST) / (platelets * sqrt(ALT))",
        "inputs": {"age": age, "ast": ast, "alt": alt, "platelets": platelets},
        "reference": "Sterling RK et al, Hepatology 2006",
    }


async def tool_calc_findrisc(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """FINDRISC 10-year type 2 diabetes risk score."""
    try:
        age = float(args.get("age_years"))
        bmi = float(args.get("bmi"))
        waist_cm = float(args.get("waist_cm"))
        sex = (args.get("sex") or "").lower()
        active = bool(args.get("physical_activity_30min_daily", False))
        veg_fruit = bool(args.get("veg_fruit_daily", False))
        bp_meds = bool(args.get("on_bp_meds", False))
        high_gluc_hx = bool(args.get("high_glucose_history", False))
        family_hx = args.get("family_diabetes_history", "none").lower()
    except (TypeError, ValueError, KeyError):
        return {"error": "required fields: age_years, bmi, waist_cm, sex, physical_activity_30min_daily, veg_fruit_daily, on_bp_meds, high_glucose_history, family_diabetes_history"}
    score = 0
    if age >= 45 and age < 55: score += 2
    elif age >= 55 and age <= 64: score += 3
    elif age > 64: score += 4
    if bmi >= 25 and bmi < 30: score += 1
    elif bmi >= 30: score += 3
    if sex == "male":
        if waist_cm >= 94 and waist_cm < 102: score += 3
        elif waist_cm >= 102: score += 4
    else:
        if waist_cm >= 80 and waist_cm < 88: score += 3
        elif waist_cm >= 88: score += 4
    if not active: score += 2
    if not veg_fruit: score += 1
    if bp_meds: score += 2
    if high_gluc_hx: score += 5
    if family_hx in ("second_degree", "second-degree"): score += 3
    elif family_hx in ("first_degree", "first-degree"): score += 5
    if score < 7: cat, risk = "Low", "1 in 100"
    elif score <= 11: cat, risk = "Slightly elevated", "1 in 25"
    elif score <= 14: cat, risk = "Moderate", "1 in 6"
    elif score <= 20: cat, risk = "High", "1 in 3"
    else: cat, risk = "Very high", "1 in 2"
    return {
        "findrisc_score": score,
        "category": cat,
        "approx_10yr_risk": risk,
        "reference": "Lindstrom J, Tuomilehto J, Diabetes Care 2003",
    }


async def tool_calc_bmi_bsa(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """BMI and BSA (DuBois) from height + weight."""
    try:
        h = float(args.get("height_cm"))
        w = float(args.get("weight_kg"))
    except (TypeError, ValueError, KeyError):
        return {"error": "height_cm and weight_kg required (numeric)"}
    if h <= 0 or w <= 0:
        return {"error": "height and weight must be positive"}
    bmi = round(w / ((h / 100) ** 2), 1)
    bsa = round(0.007184 * (w ** 0.425) * (h ** 0.725), 2)
    if bmi < 18.5: cat = "Underweight"
    elif bmi < 25: cat = "Normal weight"
    elif bmi < 30: cat = "Overweight"
    elif bmi < 35: cat = "Obese class I"
    elif bmi < 40: cat = "Obese class II"
    else: cat = "Obese class III"
    return {
        "bmi": bmi, "bmi_category": cat,
        "bsa_m2": bsa, "bsa_formula": "DuBois & DuBois 1916",
        "inputs": {"height_cm": h, "weight_kg": w},
    }


async def tool_rxnav_interactions(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Look up drug-drug interactions via NIH RxNav.

    Note: As of 2024 NIH retired the live drug-interaction API but the lookup-by-rxcui
    endpoints still resolve drug identity. This tool returns the identity resolution
    so the agent has a stable reference. Interaction reasoning belongs in the
    Drug-Supplement Check specialist.
    """
    drug = (args or {}).get("drug_name", "").strip()
    if not drug:
        return {"error": "drug_name required"}
    try:
        url = f"{RXNAV_BASE}/rxcui.json"
        data = await http_get_with_retry(url, {"name": drug, "search": "2"}, request_id=request_id)
        ids = (data.get("idGroup") or {}).get("rxnormId") or []
        if not ids:
            return {"drug_name": drug, "rxcui": None, "note": "Not found in RxNorm"}
        rxcui = ids[0]
        # Pull related drug info
        props = await http_get_with_retry(f"{RXNAV_BASE}/rxcui/{rxcui}/properties.json", request_id=request_id)
        return {
            "drug_name": drug,
            "rxcui": rxcui,
            "properties": props.get("properties"),
            "source": "NIH RxNav",
        }
    except Exception as e:
        return {"error": str(e), "drug_name": drug}


async def tool_audit_tail(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    n = int((args or {}).get("n", 25))
    n = max(1, min(n, 200))
    items = list(AUDIT_RING)[-n:]
    return {"count": len(items), "events": items}


# ---------------------------------------------------------------------------
# PDF clinical brief generation. Reports persisted to disk and served via
# GET /reports/{id}. The Report Generator specialist (or any orchestrator
# turn) calls generate_clinical_pdf, gets back {report_id, url}, and drops
# the URL in the Po chat as a clickable markdown link.
# ---------------------------------------------------------------------------

REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/tmp/lc-reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # set on deploy

REPORTS_INDEX: dict[str, dict[str, Any]] = {}


def _render_clinical_pdf(report_id: str, brief: dict[str, Any]) -> Path:
    """Render a polished clinical brief PDF to disk and return the path."""
    if not REPORTLAB_OK:
        raise RuntimeError("reportlab is not installed; add it to requirements.txt")

    out = REPORTS_DIR / f"{report_id}.pdf"
    styles = getSampleStyleSheet()

    # Brand palette
    accent = colors.HexColor("#1F3A2E")
    muted = colors.HexColor("#6B6760")
    paper = colors.HexColor("#FBFAF6")

    title_style = ParagraphStyle(
        "title", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=accent, spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "subtitle", parent=styles["Normal"], fontName="Helvetica-Oblique",
        fontSize=11, leading=14, textColor=muted, spaceAfter=14,
    )
    h_style = ParagraphStyle(
        "h", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=16, textColor=accent, spaceBefore=10, spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "body", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=10.5, leading=14, textColor=colors.black, spaceAfter=4,
    )
    bullet_style = ParagraphStyle(
        "bullet", parent=body_style, leftIndent=14, bulletIndent=2,
    )
    scope_style = ParagraphStyle(
        "scope", parent=styles["Italic"], fontName="Helvetica-Oblique",
        fontSize=9.5, leading=13, textColor=muted, spaceBefore=18,
    )

    def header_footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(accent)
        canvas.rect(0, LETTER[1] - 0.6 * inch, LETTER[0], 0.6 * inch, fill=True, stroke=0)
        canvas.setFillColor(paper)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(0.6 * inch, LETTER[1] - 0.38 * inch, "Longevity Copilot - Clinical Brief")
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(LETTER[0] - 0.6 * inch, LETTER[1] - 0.38 * inch,
                               brief.get("patient_label", "Patient"))
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(0.6 * inch, 0.4 * inch, f"Report ID: {report_id}")
        canvas.drawRightString(LETTER[0] - 0.6 * inch, 0.4 * inch,
                               f"Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
        canvas.restoreState()

    doc = BaseDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.95 * inch, bottomMargin=0.65 * inch,
        title=f"Longevity Copilot brief {report_id}",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="lc", frames=frame, onPage=header_footer)])

    story: list[Any] = []

    headline = brief.get("headline") or "Clinical brief"
    story.append(Paragraph(str(headline), title_style))
    if brief.get("subtitle"):
        story.append(Paragraph(str(brief["subtitle"]), subtitle_style))

    findings = brief.get("findings") or []
    if findings:
        story.append(Paragraph("Findings", h_style))
        rows = [["Marker", "Value", "Unit", "Source"]]
        for f in findings:
            rows.append([
                str(f.get("name", "")),
                str(f.get("value", "")),
                str(f.get("unit", "")),
                str(f.get("source", "")),
            ])
        t = Table(rows, hAlign="LEFT", colWidths=[2.2 * inch, 1.0 * inch, 0.9 * inch, 1.8 * inch])
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 9.5),
            ("TEXTCOLOR", (0, 0), (-1, 0), accent),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, accent),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#ECE6DA")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)

    for section_key, section_label in [
        ("clinical_reasoning", "Clinical reasoning"),
        ("plan", "Plan"),
        ("monitoring", "Monitoring"),
        ("genomic_context", "Genomic context"),
        ("patient_education", "Patient education"),
    ]:
        val = brief.get(section_key)
        if not val:
            continue
        story.append(Paragraph(section_label, h_style))
        if isinstance(val, list):
            for item in val:
                story.append(Paragraph("&bull; " + str(item), bullet_style))
        else:
            story.append(Paragraph(str(val), body_style))

    citations = brief.get("citations") or []
    if citations:
        story.append(Paragraph("Citations", h_style))
        for c in citations:
            story.append(Paragraph("&bull; " + str(c), bullet_style))

    scope = brief.get("scope_statement") or "For licensed-clinician review. Not for direct patient distribution without clinician sign-off."
    story.append(Paragraph(scope, scope_style))

    doc.build(story)
    return out


async def tool_generate_clinical_pdf(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Render a clinical brief to PDF. Returns {report_id, url, bytes}."""
    if not REPORTLAB_OK:
        return {"error": "reportlab is not installed on this MCP. Add it to requirements.txt and redeploy."}
    brief = args or {}
    if not brief.get("headline"):
        return {"error": "headline is required"}
    report_id = f"r-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    try:
        path = _render_clinical_pdf(report_id, brief)
    except Exception as e:  # noqa: BLE001
        return {"error": f"PDF render failed: {e}"}
    size_bytes = path.stat().st_size
    rel_url = f"/reports/{report_id}"
    full_url = f"{PUBLIC_BASE_URL}{rel_url}" if PUBLIC_BASE_URL else rel_url
    REPORTS_INDEX[report_id] = {
        "id": report_id, "path": str(path), "bytes": size_bytes,
        "created_ts": int(time.time()), "patient_label": brief.get("patient_label"),
        "headline": brief.get("headline"),
    }
    return {
        "report_id": report_id,
        "url": full_url,
        "relative_url": rel_url,
        "bytes": size_bytes,
        "markdown_link": f"[Download the clinical brief PDF]({full_url})",
    }


def _render_patient_education_pdf(report_id: str, brief: dict[str, Any]) -> Path:
    """Render a simpler patient-education PDF. Plain language, big type, warm tone."""
    if not REPORTLAB_OK:
        raise RuntimeError("reportlab is not installed; add it to requirements.txt")

    out = REPORTS_DIR / f"{report_id}.pdf"
    styles = getSampleStyleSheet()
    accent = colors.HexColor("#1F3A2E")
    muted = colors.HexColor("#6B6760")
    paper = colors.HexColor("#FBFAF6")

    title_style = ParagraphStyle("title", parent=styles["Title"], fontName="Helvetica-Bold",
                                 fontSize=22, leading=26, textColor=accent, spaceAfter=8)
    greeting_style = ParagraphStyle("greeting", parent=styles["Normal"], fontName="Helvetica",
                                    fontSize=14, leading=20, spaceAfter=10)
    h_style = ParagraphStyle("h", parent=styles["Heading2"], fontName="Helvetica-Bold",
                             fontSize=15, leading=20, textColor=accent, spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica",
                                fontSize=12.5, leading=18, textColor=colors.black, spaceAfter=8)
    bullet_style = ParagraphStyle("bullet", parent=body_style, leftIndent=18, bulletIndent=4)
    scope_style = ParagraphStyle("scope", parent=styles["Italic"], fontName="Helvetica-Oblique",
                                 fontSize=10.5, leading=15, textColor=muted, spaceBefore=18)

    def header_footer(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(accent)
        canvas.rect(0, LETTER[1] - 0.7 * inch, LETTER[0], 0.7 * inch, fill=True, stroke=0)
        canvas.setFillColor(paper)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(0.7 * inch, LETTER[1] - 0.42 * inch, "For You - Your Health Plan")
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(0.7 * inch, 0.4 * inch, f"Prepared {time.strftime('%B %d, %Y', time.gmtime())}")
        canvas.drawRightString(LETTER[0] - 0.7 * inch, 0.4 * inch, "Discuss any questions with your clinician.")
        canvas.restoreState()

    doc = BaseDocTemplate(str(out), pagesize=LETTER,
                          leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                          topMargin=1.0 * inch, bottomMargin=0.7 * inch,
                          title=f"Patient education {report_id}")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="lc", frames=frame, onPage=header_footer)])

    name = brief.get("patient_first_name") or "Friend"
    story: list[Any] = [Paragraph(f"Hi {name},", title_style)]

    intro = brief.get("intro_paragraph") or "Here is a simple version of what we talked about and what to do next."
    story.append(Paragraph(intro, greeting_style))

    findings_simple = brief.get("findings_in_plain_language") or []
    if findings_simple:
        story.append(Paragraph("What your results are showing", h_style))
        for f in findings_simple:
            story.append(Paragraph("&bull; " + str(f), bullet_style))

    plan_simple = brief.get("plan_in_plain_language") or []
    if plan_simple:
        story.append(Paragraph("What to do this month", h_style))
        for p in plan_simple:
            story.append(Paragraph("&bull; " + str(p), bullet_style))

    monitoring_simple = brief.get("when_we_recheck") or []
    if monitoring_simple:
        story.append(Paragraph("When we will recheck", h_style))
        for m in monitoring_simple:
            story.append(Paragraph("&bull; " + str(m), bullet_style))

    questions = brief.get("questions_to_bring") or []
    if questions:
        story.append(Paragraph("Questions to bring to your next visit", h_style))
        for q in questions:
            story.append(Paragraph("&bull; " + str(q), bullet_style))

    closing = brief.get("closing_paragraph") or "Reach out if anything feels off. We are here for you."
    story.append(Paragraph(closing, body_style))

    scope = brief.get("scope_statement") or "This document is for your reference, not a replacement for clinical advice. Talk to your clinician about anything that does not feel right."
    story.append(Paragraph(scope, scope_style))

    doc.build(story)
    return out


async def tool_generate_patient_education_pdf(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Render a patient-facing PDF (simpler reading level than the clinical brief)."""
    if not REPORTLAB_OK:
        return {"error": "reportlab is not installed; add it to requirements.txt"}
    brief = args or {}
    if not (brief.get("patient_first_name") or brief.get("intro_paragraph")):
        return {"error": "patient_first_name or intro_paragraph required"}
    report_id = f"pe-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    try:
        path = _render_patient_education_pdf(report_id, brief)
    except Exception as e:  # noqa: BLE001
        return {"error": f"PDF render failed: {e}"}
    size_bytes = path.stat().st_size
    rel_url = f"/reports/{report_id}"
    full_url = f"{PUBLIC_BASE_URL}{rel_url}" if PUBLIC_BASE_URL else rel_url
    REPORTS_INDEX[report_id] = {
        "id": report_id, "path": str(path), "bytes": size_bytes,
        "created_ts": int(time.time()), "patient_label": brief.get("patient_first_name"),
        "headline": "Patient education PDF",
    }
    return {
        "report_id": report_id,
        "url": full_url,
        "relative_url": rel_url,
        "bytes": size_bytes,
        "markdown_link": f"[Download your plain-language summary]({full_url})",
    }


async def tool_list_reports(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    n = int((args or {}).get("n", 25))
    items = sorted(REPORTS_INDEX.values(), key=lambda r: r["created_ts"], reverse=True)[:n]
    return {"count": len(items), "reports": items}


async def tool_list_supported_sources(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Return the breadth of supported sources: lab vendors, wearables, EHRs, biomarkers."""
    biomarker_names = sorted({v["name"] for v in VENDOR_TO_LOINC.values()})
    include_api = bool((args or {}).get("include_api_details", False))
    out = {
        "lab_vendors": {"count": len(LAB_VENDORS), "items": LAB_VENDORS},
        "wearables":   {"count": len(WEARABLE_SOURCES), "items": WEARABLE_SOURCES},
        "ehr_sources": {"count": len(EHR_SOURCES), "items": EHR_SOURCES},
        "biomarkers":  {"count": len(biomarker_names), "items": biomarker_names},
        "vendor_mappings": {"count": len(VENDOR_TO_LOINC), "note": "Vendor-code -> LOINC via normalize_biomarker."},
        "wearable_metrics_mapped": {"count": len(WEARABLE_METRIC_MAP), "note": "Vendor wearable metric -> canonical key via normalize_wearable_metric."},
    }
    if include_api:
        out["wearable_api_details"] = WEARABLE_API_DETAILS
    return out


async def tool_normalize_wearable_metric(args: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Normalize a vendor-specific wearable metric name to the canonical key."""
    vendor = (args or {}).get("vendor", "").strip().lower()
    metric = (args or {}).get("metric", "").strip().lower()
    if not vendor or not metric:
        return {"error": "vendor and metric are required"}
    key = f"{vendor}:{metric}"
    canonical = WEARABLE_METRIC_MAP.get(key)
    if canonical:
        return {"vendor_input": key, "canonical": canonical}
    return {"vendor_input": key, "canonical": None, "note": f"Not in WEARABLE_METRIC_MAP. {len(WEARABLE_METRIC_MAP)} mappings covered. Extend the map or use an aggregator (Terra/Validic/Rook)."}


# ---------------------------------------------------------------------------
# MCP protocol surface.
# ---------------------------------------------------------------------------

TOOLS = [
    {"name": "get_patient_demographics", "description": "Demographics from HAPI FHIR R4 (live) or synthetic Tyrone (fallback). PHI-minimized.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}}}},
    {"name": "get_patient_labs", "description": "Observation Bundle normalized to LOINC + UCUM. Covers 60+ vendor codes across Quest, LabCorp, Boston Heart.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}}}},
    {"name": "get_patient_medications", "description": "MedicationStatement Bundle with RxNorm coding when available.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}}}},
    {"name": "get_patient_genomics", "description": "SNP/genotype variants. Synthetic Tyrone returns MTHFR/COMT/APOE. Wire vendor APIs for production.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}}}},
    {"name": "get_wearable_snapshot", "description": "Wearable summary (HRV, RHR, sleep). Stubbed for non-Tyrone - wire Oura/Whoop/HealthKit for production.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}, "window": {"type": "string"}}}},
    {"name": "convert_units",
     "description": "Convert lab values between US conventional and SI units (glucose, cholesterol, triglycerides, creatinine, vitamin D, B12, testosterone, hemoglobin). Provide analyte for context-aware conversions.",
     "inputSchema": {"type": "object", "properties": {"value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}, "analyte": {"type": "string"}}, "required": ["value", "from_unit", "to_unit"]}},
    {"name": "normalize_panel",
     "description": "Batch normalize a full vendor lab panel to LOINC + UCUM. Pass {vendor, panel:[{code,value,unit}]}.",
     "inputSchema": {"type": "object", "properties": {"vendor": {"type": "string"}, "panel": {"type": "array"}}, "required": ["vendor", "panel"]}},
    {"name": "fhir_create_medication_statement",
     "description": "Build (and optionally POST) a FHIR R4 MedicationStatement with RxNorm coding.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}, "medication_name": {"type": "string"}, "rxnorm": {"type": "string"}, "dose": {"type": "string"}, "frequency": {"type": "string"}, "status": {"type": "string"}, "post": {"type": "boolean"}}, "required": ["medication_name"]}},
    {"name": "fhir_create_condition",
     "description": "Build (and optionally POST) a FHIR R4 Condition (diagnosis or working impression). Supports SNOMED CT + ICD-10-CM.",
     "inputSchema": {"type": "object", "properties": {"patient_id": {"type": "string"}, "text": {"type": "string"}, "snomed_code": {"type": "string"}, "icd10_code": {"type": "string"}, "clinical_status": {"type": "string"}, "verification_status": {"type": "string"}, "post": {"type": "boolean"}}, "required": ["text"]}},
    {"name": "normalize_biomarker", "description": "Vendor + vendor_code -> canonical LOINC + UCUM. 60+ codes covered.",
     "inputSchema": {"type": "object", "properties": {"vendor": {"type": "string"}, "vendor_code": {"type": "string"}}, "required": ["vendor", "vendor_code"]}},
    {"name": "fhir_passthrough", "description": "Arbitrary FHIR R4 read against the configured base. Use for resources not covered by other tools.",
     "inputSchema": {"type": "object", "properties": {"resource_type": {"type": "string"}, "params": {"type": "object"}}}},
    {"name": "calc_homa_ir", "description": "Deterministic HOMA-IR calculator. Use this so the agent never invents the math.",
     "inputSchema": {"type": "object", "properties": {"fasting_insulin_uIU_mL": {"type": "number"}, "fasting_glucose_mg_dL": {"type": "number"}}, "required": ["fasting_insulin_uIU_mL", "fasting_glucose_mg_dL"]}},
    {"name": "calc_egfr_ckdepi_2021", "description": "CKD-EPI 2021 race-free eGFR. NEJM-validated coefficients.",
     "inputSchema": {"type": "object", "properties": {"creatinine_mg_dL": {"type": "number"}, "age_years": {"type": "number"}, "sex": {"type": "string"}}, "required": ["creatinine_mg_dL", "age_years", "sex"]}},
    {"name": "calc_ascvd_10yr", "description": "ACC/AHA 2013 Pooled Cohort 10-year ASCVD risk.",
     "inputSchema": {"type": "object", "properties": {"age_years": {"type": "number"}, "sex": {"type": "string"}, "race": {"type": "string"}, "total_cholesterol_mg_dL": {"type": "number"}, "hdl_mg_dL": {"type": "number"}, "sbp_mmHg": {"type": "number"}, "treated_for_hypertension": {"type": "boolean"}, "diabetes": {"type": "boolean"}, "smoker": {"type": "boolean"}}, "required": ["age_years", "sex", "total_cholesterol_mg_dL", "hdl_mg_dL", "sbp_mmHg"]}},
    {"name": "interpret_vitals",
     "description": "Interpret a vital-signs panel. Detects critical values (BP >180/120, HR <40 or >130, SpO2 <88, RR <8 or >30, temp <35 or >39). Returns findings + critical_values + headline.",
     "inputSchema": {"type": "object", "properties": {
        "systolic_bp_mmHg": {"type": "number"},
        "diastolic_bp_mmHg": {"type": "number"},
        "heart_rate_bpm": {"type": "number"},
        "respiratory_rate_per_min": {"type": "number"},
        "spo2_pct": {"type": "number"},
        "temperature_c": {"type": "number"},
        "temperature_f": {"type": "number"},
     }}},
    {"name": "fhir_create_observation",
     "description": "Build a FHIR R4 Observation from a single LOINC + value. POSTs to FHIR base when post=true.",
     "inputSchema": {"type": "object", "properties": {
        "patient_id": {"type": "string"},
        "loinc": {"type": "string"},
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "display": {"type": "string"},
        "effective_iso": {"type": "string"},
        "post": {"type": "boolean"},
     }, "required": ["loinc", "value"]}},
    {"name": "discover",
     "description": "Return the full MCP manifest in one call: tool list, endpoints, coverage stats, sample request payloads. Useful when an agent first connects.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "simulate_lab_panel",
     "description": "Generate a synthetic lab panel for a fictional patient. Profiles: healthy, metabolic_syndrome, hashimoto, insulin_resistant. Pure synthetic data; never use for real clinical decisions.",
     "inputSchema": {"type": "object", "properties": {
        "age_years": {"type": "integer"},
        "sex": {"type": "string"},
        "profile": {"type": "string"},
        "seed": {"type": "integer"},
     }}},
    {"name": "calc_reference_ranges",
     "description": "Standard reference range + functional-medicine optimal range for a LOINC code. Pass value to get a verdict (in optimal, in reference, or outside).",
     "inputSchema": {"type": "object", "properties": {"loinc": {"type": "string"}, "value": {"type": ["number", "null"]}}, "required": ["loinc"]}},
    {"name": "chart_lab_trend",
     "description": "Render a PNG trend chart of a lab marker over time. Series is [{date,value}]. Hosts at /charts/{id} and returns a markdown_link image the agent can embed in Po chat.",
     "inputSchema": {"type": "object", "properties": {"marker": {"type": "string"}, "unit": {"type": "string"}, "loinc": {"type": "string"}, "series": {"type": "array"}}, "required": ["marker", "series"]}},
    {"name": "drug_interaction_matrix",
     "description": "Pairwise NxN interaction matrix for a list of drugs/supplements. Best-effort RxNav lookups + static KB of notable interactions.",
     "inputSchema": {"type": "object", "properties": {"drugs": {"type": "array", "items": {"type": "string"}}}, "required": ["drugs"]}},
    {"name": "fhir_create_diagnostic_report",
     "description": "Build a FHIR R4 DiagnosticReport from a clinical brief. Returns the resource. If post=true, posts it to the configured FHIR base and returns the server-assigned id. This is how the agent writes findings back to the EHR.",
     "inputSchema": {"type": "object", "properties": {
        "patient_id": {"type": "string"},
        "headline": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object"}},
        "clinical_reasoning": {"type": "string"},
        "plan": {"type": ["array", "string"]},
        "monitoring": {"type": ["array", "string"]},
        "scope_statement": {"type": "string"},
        "post": {"type": "boolean", "description": "If true, POST the DiagnosticReport to the configured FHIR base."},
     }, "required": ["headline"]}},
    {"name": "calc_fib4", "description": "FIB-4 score for advanced liver fibrosis. Inputs: age, AST, ALT, platelets.",
     "inputSchema": {"type": "object", "properties": {"age_years": {"type": "number"}, "ast_U_L": {"type": "number"}, "alt_U_L": {"type": "number"}, "platelets_10e9_L": {"type": "number"}}, "required": ["age_years", "ast_U_L", "alt_U_L", "platelets_10e9_L"]}},
    {"name": "calc_findrisc", "description": "FINDRISC 10-year type 2 diabetes risk.",
     "inputSchema": {"type": "object", "properties": {"age_years": {"type": "number"}, "bmi": {"type": "number"}, "waist_cm": {"type": "number"}, "sex": {"type": "string"}, "physical_activity_30min_daily": {"type": "boolean"}, "veg_fruit_daily": {"type": "boolean"}, "on_bp_meds": {"type": "boolean"}, "high_glucose_history": {"type": "boolean"}, "family_diabetes_history": {"type": "string"}}, "required": ["age_years", "bmi", "waist_cm", "sex"]}},
    {"name": "calc_bmi_bsa", "description": "BMI + body surface area (DuBois).",
     "inputSchema": {"type": "object", "properties": {"height_cm": {"type": "number"}, "weight_kg": {"type": "number"}}, "required": ["height_cm", "weight_kg"]}},
    {"name": "rxnav_interactions", "description": "Resolve a drug name to RxCUI + RxNorm properties via NIH RxNav. Agent should still route to Drug-Supplement Check for clinical interaction reasoning.",
     "inputSchema": {"type": "object", "properties": {"drug_name": {"type": "string"}}, "required": ["drug_name"]}},
    {"name": "audit_tail", "description": "Return the last N audit events from this MCP. Use for debugging routing failures.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    {"name": "list_supported_sources", "description": "List all supported lab vendors, wearables, EHR sources, biomarkers, and wearable metric mappings. Pass include_api_details=true for per-device auth + endpoint + metric notes.",
     "inputSchema": {"type": "object", "properties": {"include_api_details": {"type": "boolean"}}}},
    {"name": "normalize_wearable_metric", "description": "Normalize a vendor-specific wearable metric (e.g., 'oura:hrv_rmssd' -> 'hrv_rmssd_ms_mean'). 28+ mappings covered.",
     "inputSchema": {"type": "object", "properties": {"vendor": {"type": "string"}, "metric": {"type": "string"}}, "required": ["vendor", "metric"]}},
    {"name": "generate_clinical_pdf",
     "description": "Render a clinical brief to a downloadable PDF. Provide headline, findings (array of {name,value,unit,source}), clinical_reasoning, plan, monitoring, genomic_context, patient_education, citations, scope_statement, patient_label. Returns a markdown_link the agent should drop in chat.",
     "inputSchema": {"type": "object", "properties": {
        "headline": {"type": "string"},
        "subtitle": {"type": "string"},
        "patient_label": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object"}},
        "clinical_reasoning": {"type": "string"},
        "plan": {"type": ["array", "string"]},
        "monitoring": {"type": ["array", "string"]},
        "genomic_context": {"type": ["array", "string"]},
        "patient_education": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "scope_statement": {"type": "string"},
     }, "required": ["headline"]}},
    {"name": "generate_patient_education_pdf",
     "description": "Render a patient-facing plain-language PDF (~6th grade reading level). Provide patient_first_name, intro_paragraph, findings_in_plain_language[], plan_in_plain_language[], when_we_recheck[], questions_to_bring[], closing_paragraph. Returns markdown_link the agent drops in chat.",
     "inputSchema": {"type": "object", "properties": {
        "patient_first_name": {"type": "string"},
        "intro_paragraph": {"type": "string"},
        "findings_in_plain_language": {"type": "array", "items": {"type": "string"}},
        "plan_in_plain_language": {"type": "array", "items": {"type": "string"}},
        "when_we_recheck": {"type": "array", "items": {"type": "string"}},
        "questions_to_bring": {"type": "array", "items": {"type": "string"}},
        "closing_paragraph": {"type": "string"},
     }}},
    {"name": "list_reports", "description": "List recently generated PDF reports.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
]

TOOL_DISPATCH = {
    "get_patient_demographics": tool_get_patient_demographics,
    "get_patient_labs":          tool_get_patient_labs,
    "get_patient_medications":   tool_get_patient_medications,
    "get_patient_genomics":      tool_get_patient_genomics,
    "get_wearable_snapshot":     tool_get_wearable_snapshot,
    "convert_units":             tool_convert_units,
    "normalize_panel":           tool_normalize_panel,
    "fhir_create_medication_statement": tool_fhir_create_medication_statement,
    "fhir_create_condition":     tool_fhir_create_condition,
    "normalize_biomarker":       tool_normalize_biomarker,
    "fhir_passthrough":          tool_fhir_passthrough,
    "calc_homa_ir":              tool_calc_homa_ir,
    "calc_egfr_ckdepi_2021":     tool_calc_egfr_ckdepi_2021,
    "calc_ascvd_10yr":           tool_calc_ascvd_10yr,
    "interpret_vitals":          tool_interpret_vitals,
    "fhir_create_observation":   tool_fhir_create_observation,
    "discover":                  tool_discover,
    "simulate_lab_panel":        tool_simulate_lab_panel,
    "calc_reference_ranges":     tool_calc_reference_ranges,
    "chart_lab_trend":           tool_chart_lab_trend,
    "drug_interaction_matrix":   tool_drug_interaction_matrix,
    "fhir_create_diagnostic_report": tool_fhir_create_diagnostic_report,
    "calc_fib4":                 tool_calc_fib4,
    "calc_findrisc":             tool_calc_findrisc,
    "calc_bmi_bsa":              tool_calc_bmi_bsa,
    "rxnav_interactions":        tool_rxnav_interactions,
    "audit_tail":                tool_audit_tail,
    "list_supported_sources":    tool_list_supported_sources,
    "normalize_wearable_metric": tool_normalize_wearable_metric,
    "generate_clinical_pdf":     tool_generate_clinical_pdf,
    "generate_patient_education_pdf": tool_generate_patient_education_pdf,
    "list_reports":              tool_list_reports,
}

SERVER_INFO = {"name": "longevity-copilot-mcp", "version": "1.7.0"}
PROTOCOL_VERSION = "2025-06-18"


app = FastAPI(title="Longevity Copilot MCP", version="1.7.0")


@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    """Permissive CORS for browser-based Po extensions and dashboards."""
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Longevity Copilot MCP",
        "version": "1.1.0",
        "transport": "streamable-http",
        "endpoint": "/mcp",
        "fhir_base": HAPI_BASE,
        "auth": "bearer" if MCP_BEARER_TOKEN else "open",
        "tools": [t["name"] for t in TOOLS],
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Health check with dependency probe."""
    fhir_ok = False
    fhir_latency_ms = None
    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{HAPI_BASE}/metadata?_summary=true")
            fhir_ok = r.status_code == 200
            fhir_latency_ms = int((time.time() - t0) * 1000)
    except Exception:
        pass
    return {
        "status": "ok",
        "ts": int(time.time()),
        "version": "1.1.0",
        "deps": {
            "hapi_fhir": {"ok": fhir_ok, "latency_ms": fhir_latency_ms, "base": HAPI_BASE},
        },
    }


async def _handle_rpc(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    method = payload.get("method")
    rpc_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "initialize":
        audit(request_id, "rpc.initialize")
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            },
        }
    if method == "notifications/initialized":
        return {}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_DISPATCH.get(name)
        if not handler:
            audit(request_id, "tool.call.unknown", name=name)
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        t0 = time.time()
        try:
            result = await handler(args, request_id)
            latency = int((time.time() - t0) * 1000)
            has_err = isinstance(result, dict) and "error" in result
            audit(request_id, "tool.call.ok", name=name, latency_ms=latency, has_error=has_err)
            metrics_record(name, latency, has_err)
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]},
            }
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - t0) * 1000)
            audit(request_id, "tool.call.exception", name=name, latency_ms=latency, err=str(exc))
            metrics_record(name, latency, True)
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": str(exc)}}

    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: str | None = Header(default=None)) -> Any:
    request_id = str(uuid.uuid4())
    require_auth(authorization, request_id)
    try:
        body = await request.json()
    except Exception:
        audit(request_id, "rpc.parse_error")
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    response = await _handle_rpc(body, request_id)
    if not response:
        return Response(status_code=204)

    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        async def stream():
            yield f"id: {request_id}\n"
            yield "event: message\n"
            yield f"data: {json.dumps(response, default=str)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"x-request-id": request_id})

    return JSONResponse(response, headers={"x-request-id": request_id})


@app.get("/mcp")
async def mcp_get() -> dict[str, Any]:
    return {"transport": "streamable-http", "post": "send JSON-RPC 2.0 to this endpoint"}


@app.get("/catalog")
async def catalog() -> Any:
    """Serve the judge-friendly browser catalog from disk if present."""
    here = Path(__file__).parent
    html = here / "catalog.html"
    if html.exists():
        return FileResponse(str(html), media_type="text/html")
    return JSONResponse({"error": "catalog.html missing"}, status_code=404)


@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    """A2A v1 agent card for direct discovery (in addition to Po marketplace)."""
    public = PUBLIC_BASE_URL or ""
    return {
        "name": "Longevity Copilot MCP",
        "description": "Data spine for longevity, functional, and concierge clinics. Normalizes labs, wearables, genomics, EHRs to FHIR R4 + LOINC + UCUM. Provides clinical calculators (HOMA-IR, eGFR, ASCVD) and patient-ready PDF brief generation.",
        "version": "1.1.0",
        "protocols": ["mcp/streamable-http", "json-rpc-2.0"],
        "auth": "bearer" if MCP_BEARER_TOKEN else "open",
        "endpoints": {
            "mcp": (public + "/mcp") if public else "/mcp",
            "catalog": (public + "/catalog") if public else "/catalog",
            "reports": (public + "/reports/{id}") if public else "/reports/{id}",
            "health": (public + "/healthz") if public else "/healthz",
        },
        "skills": [{"id": t["name"], "description": t["description"]} for t in TOOLS],
        "coverage": {
            "lab_vendors": len(LAB_VENDORS),
            "wearables": len(WEARABLE_SOURCES),
            "ehr_sources": len(EHR_SOURCES),
            "biomarkers": len({v["name"] for v in VENDOR_TO_LOINC.values()}),
            "vendor_mappings": len(VENDOR_TO_LOINC),
        },
        "links": {
            "marketplace": "https://app.promptopinion.ai",
            "docs": "/",
        },
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus text-format metrics. Per-tool calls, errors, avg latency."""
    lines = [
        "# HELP lc_mcp_tool_calls_total Total tool calls",
        "# TYPE lc_mcp_tool_calls_total counter",
    ]
    for name, m in sorted(METRICS.items()):
        lines.append(f'lc_mcp_tool_calls_total{{tool="{name}"}} {m["calls"]}')
    lines += [
        "# HELP lc_mcp_tool_errors_total Total tool-call errors",
        "# TYPE lc_mcp_tool_errors_total counter",
    ]
    for name, m in sorted(METRICS.items()):
        lines.append(f'lc_mcp_tool_errors_total{{tool="{name}"}} {m["errors"]}')
    lines += [
        "# HELP lc_mcp_tool_avg_latency_ms Average tool latency (ms)",
        "# TYPE lc_mcp_tool_avg_latency_ms gauge",
    ]
    for name, m in sorted(METRICS.items()):
        avg = (m["total_ms"] / m["calls"]) if m["calls"] else 0
        lines.append(f'lc_mcp_tool_avg_latency_ms{{tool="{name}"}} {avg:.2f}')
    lines += [
        "# HELP lc_mcp_audit_events_in_ring Audit events currently in memory ring",
        "# TYPE lc_mcp_audit_events_in_ring gauge",
        f"lc_mcp_audit_events_in_ring {len(AUDIT_RING)}",
        "# HELP lc_mcp_reports_generated Total PDF reports generated",
        "# TYPE lc_mcp_reports_generated counter",
        f"lc_mcp_reports_generated {len(REPORTS_INDEX)}",
        "# HELP lc_mcp_charts_generated Total charts generated",
        "# TYPE lc_mcp_charts_generated counter",
        f"lc_mcp_charts_generated {len(CHARTS_INDEX)}",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/dashboard")
async def dashboard() -> Any:
    """Admin dashboard: health, audit tail, recent reports, active tools."""
    audit_recent = list(AUDIT_RING)[-20:]
    reports_recent = sorted(REPORTS_INDEX.values(), key=lambda r: r["created_ts"], reverse=True)[:5]

    def esc(s: Any) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    audit_rows = ""
    for e in reversed(audit_recent):
        ts = time.strftime("%H:%M:%S", time.gmtime(e["ts"] / 1000))
        kind = e["kind"]
        color = "#1F3A2E" if "ok" in kind or kind == "rpc.initialize" else "#B5563C"
        details = ", ".join(f"{k}={esc(v)}" for k, v in e.items() if k not in ("ts", "kind", "request_id"))
        audit_rows += f"<tr><td><code>{ts}</code></td><td style='color:{color}'>{esc(kind)}</td><td class=muted>{details}</td></tr>"

    report_rows = ""
    for r in reports_recent:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_ts"]))
        report_rows += (
            f"<tr><td><code>{esc(r['id'])}</code></td><td>{esc(r.get('headline','?'))}</td>"
            f"<td class=muted>{esc(r.get('patient_label',''))}</td><td class=muted>{ts}</td>"
            f"<td><a href='/reports/{esc(r['id'])}'>open PDF</a></td></tr>"
        )

    # health probe
    fhir_ok = False
    fhir_latency = "?"
    try:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{HAPI_BASE}/metadata?_summary=true")
            fhir_ok = r.status_code == 200
            fhir_latency = f"{int((time.time() - t0) * 1000)} ms"
    except Exception:
        pass

    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Longevity Copilot MCP - Admin</title>
<meta http-equiv='refresh' content='10'>
<style>
  body {{ font: 14px/1.5 system-ui, Inter, sans-serif; background: #F7F3EC; color: #1C1B18; padding: 24px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #1F3A2E; font-style: italic; font-weight: 500; font-size: 26px; margin-bottom: 4px; }}
  h2 {{ color: #1F3A2E; font-size: 13px; letter-spacing: 0.18em; text-transform: uppercase; margin: 22px 0 8px; }}
  .row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .stat {{ background: #FBFAF6; border: 1px solid #ECE6DA; border-radius: 10px; padding: 12px 16px; }}
  .stat .num {{ font-size: 24px; font-weight: 600; color: #1F3A2E; }}
  .stat .lbl {{ font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: #6B6760; }}
  table {{ width: 100%; border-collapse: collapse; background: #FBFAF6; border: 1px solid #ECE6DA; border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 6px 12px; border-bottom: 1px solid #ECE6DA; font-size: 13px; }}
  th {{ background: #F7F3EC; color: #6B6760; font-weight: 600; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }}
  .muted {{ color: #6B6760; font-size: 12px; }}
  .good {{ color: #1F3A2E; font-weight: 700; }}
  .bad {{ color: #B5563C; font-weight: 700; }}
  code {{ font: 12px ui-monospace, monospace; color: #1F3A2E; }}
  a {{ color: #1F3A2E; }}
  .sub {{ color: #6B6760; font-size: 12px; margin-bottom: 16px; }}
</style></head><body>
<h1>Longevity Copilot MCP - admin</h1>
<div class='sub'>auto-refresh every 10s - v1.2 - {esc(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))}</div>

<div class='row'>
  <div class='stat'><div class='lbl'>Tools</div><div class='num'>{len(TOOLS)}</div></div>
  <div class='stat'><div class='lbl'>Vendor codes</div><div class='num'>{len(VENDOR_TO_LOINC)}</div></div>
  <div class='stat'><div class='lbl'>Audit events (ring)</div><div class='num'>{len(AUDIT_RING)}</div></div>
  <div class='stat'><div class='lbl'>Reports generated</div><div class='num'>{len(REPORTS_INDEX)}</div></div>
</div>

<h2>Health</h2>
<table>
  <tr><th>Component</th><th>Status</th><th>Detail</th></tr>
  <tr><td>MCP server</td><td class='good'>up</td><td class='muted'>v1.2 - bearer auth {'on' if MCP_BEARER_TOKEN else 'off'}</td></tr>
  <tr><td>HAPI FHIR base</td><td class='{'good' if fhir_ok else 'bad'}'>{'up' if fhir_ok else 'down'}</td><td class='muted'>{esc(HAPI_BASE)} - {fhir_latency}</td></tr>
  <tr><td>reportlab</td><td class='{'good' if REPORTLAB_OK else 'bad'}'>{'loaded' if REPORTLAB_OK else 'missing'}</td><td class='muted'>PDF generation</td></tr>
  <tr><td>Audit file</td><td class='good'>open</td><td class='muted'>{esc(AUDIT_LOG_PATH)}</td></tr>
</table>

<h2>Audit tail (last 20)</h2>
<table><tr><th>Time</th><th>Event</th><th>Detail</th></tr>{audit_rows or '<tr><td colspan=3 class=muted>No events yet.</td></tr>'}</table>

<h2>Recent reports (last 5)</h2>
<table><tr><th>Report ID</th><th>Headline</th><th>Patient</th><th>When</th><th>Open</th></tr>{report_rows or '<tr><td colspan=5 class=muted>No reports yet.</td></tr>'}</table>

</body></html>
"""
    return Response(content=html, media_type="text/html")


@app.get("/scorecard/{patient_id}")
async def scorecard(patient_id: str) -> Any:
    """Patient scorecard - one-page HTML dashboard for a patient.

    Pulls demographics + labs + medications + genomics + wearables via the MCP's
    own tools and renders them inline. Useful for a quick visual sanity check
    that the MCP data flow is healthy.
    """
    request_id = f"scorecard-{uuid.uuid4().hex[:8]}"
    demo = await tool_get_patient_demographics({"patient_id": patient_id}, request_id)
    labs = await tool_get_patient_labs({"patient_id": patient_id}, request_id)
    meds = await tool_get_patient_medications({"patient_id": patient_id}, request_id)
    gen = await tool_get_patient_genomics({"patient_id": patient_id}, request_id)
    wear = await tool_get_wearable_snapshot({"patient_id": patient_id, "window": "last_7_days"}, request_id)

    def esc(s: Any) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    d = (demo or {}).get("data") or {}
    lab_rows = ""
    for lab in (labs.get("results") or []):
        lab_rows += (
            f"<tr><td>{esc(lab.get('name'))}</td>"
            f"<td><code>{esc(lab.get('loinc'))}</code></td>"
            f"<td><b>{esc(lab.get('value'))}</b> {esc(lab.get('unit'))}</td>"
            f"<td class=muted>{esc(lab.get('source'))}</td>"
            f"<td class=muted>{esc(lab.get('date'))}</td></tr>"
        )
    med_rows = ""
    for m in (meds.get("medications") or []):
        med_rows += (
            f"<tr><td><b>{esc(m.get('name'))}</b></td>"
            f"<td>{esc(m.get('dose'))} {esc(m.get('frequency',''))}</td>"
            f"<td class=muted>RxCUI {esc(m.get('rxnorm'))}</td></tr>"
        )
    gen_rows = ""
    for g in (gen.get("variants") or []):
        gen_rows += (
            f"<tr><td><b>{esc(g.get('gene'))}</b></td>"
            f"<td><code>{esc(g.get('rsid'))}</code></td>"
            f"<td>{esc(g.get('genotype'))}</td>"
            f"<td class=muted>{esc(g.get('phenotype'))}</td></tr>"
        )
    snap = (wear.get("snapshot") or {})
    metrics = snap.get("metrics") or {}
    metric_pills = ""
    for k, v in metrics.items():
        metric_pills += f"<span class=pill><b>{esc(k)}</b> {esc(v)}</span>"

    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Scorecard - {esc(d.get('firstName',''))} {esc(d.get('lastNameInitial',''))}</title>
<style>
  body {{ font: 14px/1.5 system-ui, -apple-system, Inter, sans-serif; background: #F7F3EC; color: #1C1B18; padding: 24px; max-width: 1100px; margin: 0 auto; }}
  h1 {{ color: #1F3A2E; font-style: italic; font-weight: 500; font-size: 28px; margin-bottom: 4px; }}
  h2 {{ color: #1F3A2E; font-size: 14px; letter-spacing: 0.18em; text-transform: uppercase; margin: 24px 0 10px; }}
  .sub {{ color: #6B6760; margin-bottom: 16px; }}
  .card {{ background: #FBFAF6; border: 1px solid #ECE6DA; border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #ECE6DA; font-size: 13px; }}
  th {{ color: #6B6760; font-weight: 600; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; }}
  .muted {{ color: #6B6760; }}
  code {{ font: 12px ui-monospace, SFMono-Regular, monospace; color: #1F3A2E; background: #F7F3EC; padding: 1px 6px; border-radius: 4px; }}
  .pill {{ display: inline-block; background: #F7F3EC; border: 1px solid #ECE6DA; border-radius: 999px; padding: 4px 12px; margin: 4px 6px 0 0; font-size: 12px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .footer {{ color: #6B6760; font-size: 12px; margin-top: 20px; text-align: center; }}
</style>
</head><body>
<h1>{esc(d.get('firstName',''))} {esc(d.get('lastNameInitial',''))}</h1>
<div class='sub'>DOB {esc(d.get('dob','?'))} - {esc(d.get('sex','?'))} - source: {esc(demo.get('source','?'))}</div>

<h2>Labs ({len(labs.get('results') or [])} markers, normalized to LOINC)</h2>
<div class='card'><table><tr><th>Marker</th><th>LOINC</th><th>Value</th><th>Source</th><th>Date</th></tr>{lab_rows}</table></div>

<div class='grid2'>
  <div>
    <h2>Medications ({len(meds.get('medications') or [])})</h2>
    <div class='card'><table><tr><th>Drug</th><th>Dose</th><th>Code</th></tr>{med_rows}</table></div>
  </div>
  <div>
    <h2>Genomics ({len(gen.get('variants') or [])})</h2>
    <div class='card'><table><tr><th>Gene</th><th>rsID</th><th>Genotype</th><th>Phenotype</th></tr>{gen_rows}</table></div>
  </div>
</div>

<h2>Wearables ({esc(snap.get('source','?'))}, {esc(snap.get('window','?'))})</h2>
<div class='card'>{metric_pills or '<span class=muted>No wearable data</span>'}</div>

<div class='footer'>
  Generated by Longevity Copilot MCP. For licensed-clinician review. Not for direct patient distribution without sign-off.
</div>
</body></html>
"""
    return Response(content=html, media_type="text/html")


@app.get("/charts/{chart_id}")
async def get_chart(chart_id: str) -> Any:
    rec = CHARTS_INDEX.get(chart_id)
    path = Path(rec["path"]) if rec else (CHARTS_DIR / f"{chart_id}.png")
    if not path.exists():
        raise HTTPException(status_code=404, detail="chart not found")
    return FileResponse(str(path), media_type="image/png")


@app.get("/reports/{report_id}")
async def get_report(report_id: str) -> Any:
    rec = REPORTS_INDEX.get(report_id)
    if not rec:
        candidate = REPORTS_DIR / f"{report_id}.pdf"
        if not candidate.exists():
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(str(candidate), media_type="application/pdf",
                            filename=f"longevity-copilot-brief-{report_id}.pdf")
    return FileResponse(rec["path"], media_type="application/pdf",
                        filename=f"longevity-copilot-brief-{report_id}.pdf")
