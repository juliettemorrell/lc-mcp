"""
Offline unit tests for the MCP. Calls each calculator function directly so
the math is verified without spinning up an HTTP server.

Run:  python -m pytest test_units.py -v
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

import server


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ----- vendor map sanity --------------------------------------------------

def test_vendor_map_count():
    assert len(server.VENDOR_TO_LOINC) >= 100


def test_vendor_map_loinc_consistency():
    """The same canonical biomarker must always map to the same LOINC across vendors."""
    loinc_by_name: dict[str, str] = {}
    for key, val in server.VENDOR_TO_LOINC.items():
        name = val["name"]
        if name in loinc_by_name:
            assert loinc_by_name[name] == val["loinc"], (
                f"Inconsistent LOINC for {name}: {loinc_by_name[name]} vs {val['loinc']} (key={key})"
            )
        else:
            loinc_by_name[name] = val["loinc"]


def test_vendor_breadth():
    assert len(server.LAB_VENDORS) >= 12
    assert len(server.WEARABLE_SOURCES) >= 12
    assert len(server.EHR_SOURCES) >= 12


# ----- HOMA-IR ------------------------------------------------------------

def test_homa_ir_tyrone():
    r = _run(server.tool_calc_homa_ir({"fasting_insulin_uIU_mL": 7.2, "fasting_glucose_mg_dL": 88}, "t"))
    assert math.isclose(r["homa_ir"], 1.56, abs_tol=0.01)
    assert "Within reference" in r["interpretation"] or "Optimal" in r["interpretation"]


def test_homa_ir_high():
    r = _run(server.tool_calc_homa_ir({"fasting_insulin_uIU_mL": 20, "fasting_glucose_mg_dL": 110}, "t"))
    assert r["homa_ir"] > 5.0
    assert r["interpretation"] == "Insulin resistance"


def test_homa_ir_missing():
    r = _run(server.tool_calc_homa_ir({}, "t"))
    assert "error" in r


# ----- eGFR ---------------------------------------------------------------

def test_egfr_ckd_g3a():
    r = _run(server.tool_calc_egfr_ckdepi_2021({"creatinine_mg_dL": 1.4, "age_years": 70, "sex": "female"}, "t"))
    assert 35 <= r["egfr_mL_min_1_73m2"] <= 45
    assert "G3" in r["ckd_stage"]


def test_egfr_normal_young():
    r = _run(server.tool_calc_egfr_ckdepi_2021({"creatinine_mg_dL": 1.05, "age_years": 26, "sex": "male"}, "t"))
    assert r["egfr_mL_min_1_73m2"] >= 95
    assert r["ckd_stage"].startswith("G1")


# ----- ASCVD --------------------------------------------------------------

def test_ascvd_male_low_risk():
    r = _run(server.tool_calc_ascvd_10yr({
        "age_years": 55, "sex": "male", "race": "white",
        "total_cholesterol_mg_dL": 213, "hdl_mg_dL": 50, "sbp_mmHg": 120,
        "treated_for_hypertension": False, "diabetes": False, "smoker": False,
    }, "t"))
    assert math.isclose(r["ascvd_10yr_risk_pct"], 5.4, abs_tol=1.0)


def test_ascvd_smoker_bumps_risk():
    a = _run(server.tool_calc_ascvd_10yr({
        "age_years": 55, "sex": "male", "race": "white",
        "total_cholesterol_mg_dL": 213, "hdl_mg_dL": 50, "sbp_mmHg": 120,
        "treated_for_hypertension": False, "diabetes": False, "smoker": False,
    }, "t"))
    b = _run(server.tool_calc_ascvd_10yr({
        "age_years": 55, "sex": "male", "race": "white",
        "total_cholesterol_mg_dL": 213, "hdl_mg_dL": 50, "sbp_mmHg": 120,
        "treated_for_hypertension": False, "diabetes": False, "smoker": True,
    }, "t"))
    assert b["ascvd_10yr_risk_pct"] > a["ascvd_10yr_risk_pct"]


# ----- FIB-4 --------------------------------------------------------------

def test_fib4_indeterminate():
    r = _run(server.tool_calc_fib4({"age_years": 50, "ast_U_L": 45, "alt_U_L": 40, "platelets_10e9_L": 220}, "t"))
    assert math.isclose(r["fib4"], 1.62, abs_tol=0.05)
    assert "Indeterminate" in r["interpretation"]


def test_fib4_high():
    r = _run(server.tool_calc_fib4({"age_years": 65, "ast_U_L": 90, "alt_U_L": 50, "platelets_10e9_L": 150}, "t"))
    assert r["fib4"] > 3.0
    assert "High risk" in r["interpretation"]


# ----- BMI / BSA ---------------------------------------------------------

def test_bmi_bsa():
    r = _run(server.tool_calc_bmi_bsa({"height_cm": 180, "weight_kg": 80}, "t"))
    assert math.isclose(r["bmi"], 24.7, abs_tol=0.1)
    assert math.isclose(r["bsa_m2"], 2.0, abs_tol=0.05)
    assert r["bmi_category"] == "Normal weight"


def test_bmi_obese():
    r = _run(server.tool_calc_bmi_bsa({"height_cm": 170, "weight_kg": 100}, "t"))
    assert r["bmi"] > 30
    assert "Obese" in r["bmi_category"]


# ----- FINDRISC ----------------------------------------------------------

def test_findrisc_low():
    r = _run(server.tool_calc_findrisc({
        "age_years": 30, "bmi": 22, "waist_cm": 80, "sex": "male",
        "physical_activity_30min_daily": True, "veg_fruit_daily": True,
        "on_bp_meds": False, "high_glucose_history": False,
        "family_diabetes_history": "none",
    }, "t"))
    assert r["findrisc_score"] == 0
    assert r["category"] == "Low"


def test_findrisc_high():
    r = _run(server.tool_calc_findrisc({
        "age_years": 60, "bmi": 28, "waist_cm": 100, "sex": "male",
        "physical_activity_30min_daily": False, "veg_fruit_daily": False,
        "on_bp_meds": False, "high_glucose_history": False,
        "family_diabetes_history": "first_degree",
    }, "t"))
    assert r["findrisc_score"] >= 12


# ----- normalize_biomarker -----------------------------------------------

def test_normalize_known():
    r = _run(server.tool_normalize_biomarker({"vendor": "quest", "vendor_code": "HBA1C"}, "t"))
    assert r["canonical"]["loinc"] == "4548-4"


def test_normalize_unknown():
    r = _run(server.tool_normalize_biomarker({"vendor": "made_up", "vendor_code": "XX"}, "t"))
    assert r["canonical"] is None


# ----- PDF generation ----------------------------------------------------

def test_pdf_renders():
    r = _run(server.tool_generate_clinical_pdf({
        "headline": "Test brief",
        "findings": [{"name": "HbA1c", "value": 5.4, "unit": "%", "source": "Quest"}],
        "plan": ["Recheck in 90 days"],
    }, "t"))
    assert "report_id" in r
    assert "url" in r


def test_pdf_missing_headline():
    r = _run(server.tool_generate_clinical_pdf({}, "t"))
    assert "error" in r


# ----- synthetic Tyrone fixture sanity ------------------------------------

def test_tyrone_labs_have_loinc():
    for lab in server.SYNTHETIC["tyrone"]["labs"]:
        assert "loinc" in lab and lab["loinc"]
        assert "value" in lab


def test_tyrone_has_methylation_genomics():
    rsids = [g["rsid"] for g in server.SYNTHETIC["tyrone"]["genomics"]]
    assert "rs1801133" in rsids  # MTHFR C677T
    assert "rs4680" in rsids     # COMT Val158Met


# ----- list_supported_sources --------------------------------------------

def test_list_sources_counts():
    r = _run(server.tool_list_supported_sources({}, "t"))
    assert r["lab_vendors"]["count"] >= 12
    assert r["wearables"]["count"] >= 12
    assert r["ehr_sources"]["count"] >= 12
    assert r["biomarkers"]["count"] >= 70
    assert r["vendor_mappings"]["count"] >= 100


# ----- discover -----------------------------------------------------------

def test_discover_returns_full_manifest():
    r = _run(server.tool_discover({}, "t"))
    assert r["server"]["name"] == "longevity-copilot-mcp"
    assert r["tool_count"] >= 26
    assert "mcp" in r["endpoints"]
    assert "metrics" in r["endpoints"]
    assert r["coverage"]["calculators"] >= 6


# ----- simulate_lab_panel ------------------------------------------------

def test_simulate_lab_panel_healthy_seeded():
    a = _run(server.tool_simulate_lab_panel({"profile": "healthy", "seed": 42}, "t"))
    b = _run(server.tool_simulate_lab_panel({"profile": "healthy", "seed": 42}, "t"))
    assert a["panel"][0]["value"] == b["panel"][0]["value"]  # deterministic via seed


def test_simulate_lab_panel_hashimoto_adds_tpo():
    r = _run(server.tool_simulate_lab_panel({"profile": "hashimoto", "seed": 1}, "t"))
    names = [p["name"] for p in r["panel"]]
    assert "TPO antibodies" in names


# ----- interpret_vitals --------------------------------------------------

def test_interpret_vitals_normal():
    r = _run(server.tool_interpret_vitals({
        "systolic_bp_mmHg": 118, "diastolic_bp_mmHg": 76,
        "heart_rate_bpm": 68, "spo2_pct": 98, "temperature_c": 36.7,
    }, "t"))
    assert not r["has_critical"]
    assert "within reference" in r["headline"].lower() or "non-critical" in r["headline"].lower()


def test_interpret_vitals_critical_bp():
    r = _run(server.tool_interpret_vitals({
        "systolic_bp_mmHg": 195, "diastolic_bp_mmHg": 125,
    }, "t"))
    assert r["has_critical"]
    assert "Critical value" in r["headline"]
    assert any("Systolic" in c or "Diastolic" in c for c in r["critical_values"])


def test_interpret_vitals_low_spo2():
    r = _run(server.tool_interpret_vitals({"spo2_pct": 85}, "t"))
    assert r["has_critical"]


def test_interpret_vitals_map():
    r = _run(server.tool_interpret_vitals({"systolic_bp_mmHg": 120, "diastolic_bp_mmHg": 80}, "t"))
    assert "mean_arterial_pressure" in r["extras"]
    # MAP = (120 + 160)/3 = 93.3
    assert 90 <= r["extras"]["mean_arterial_pressure"]["value"] <= 100


# ----- fhir_create_observation (dry-run) ---------------------------------

def test_fhir_create_observation_builds_resource():
    r = _run(server.tool_fhir_create_observation({
        "loinc": "4548-4", "value": 5.4, "unit": "%",
        "patient_id": "tyrone-215", "post": False,
    }, "t"))
    assert not r["posted"]
    assert r["resource"]["resourceType"] == "Observation"
    assert r["resource"]["code"]["coding"][0]["code"] == "4548-4"
    assert r["resource"]["valueQuantity"]["value"] == 5.4


def test_fhir_create_observation_missing_value():
    r = _run(server.tool_fhir_create_observation({"loinc": "4548-4"}, "t"))
    assert "error" in r


# ----- fhir_create_diagnostic_report (dry-run) ---------------------------

def test_fhir_diagnostic_report_dry_run():
    r = _run(server.tool_fhir_create_diagnostic_report({
        "patient_id": "tyrone-215",
        "headline": "Test brief",
        "findings": [{"name": "HbA1c", "value": 5.4, "unit": "%"}],
        "plan": ["Recheck in 90 days"],
        "post": False,
    }, "t"))
    assert not r["posted"]
    assert r["resource"]["resourceType"] == "DiagnosticReport"
    assert r["resource"]["subject"]["reference"] == "Patient/tyrone-215"


# ----- calc_reference_ranges ----------------------------------------------

def test_reference_ranges_in_optimal():
    r = _run(server.tool_calc_reference_ranges({"loinc": "4548-4", "value": 5.0}, "t"))
    assert r["in_optimal"]


def test_reference_ranges_outside():
    r = _run(server.tool_calc_reference_ranges({"loinc": "62292-8", "value": 20}, "t"))
    assert not r["in_reference"]


# ----- drug interaction matrix --------------------------------------------

def test_drug_interaction_matrix_known_pair():
    r = _run(server.tool_drug_interaction_matrix({"drugs": ["Metformin", "Berberine"]}, "t"))
    # Should find the moderate interaction
    sevs = [r["matrix"][0][1]["severity"], r["matrix"][1][0]["severity"]]
    assert "moderate" in sevs


def test_drug_interaction_matrix_too_few():
    r = _run(server.tool_drug_interaction_matrix({"drugs": ["Metformin"]}, "t"))
    assert "error" in r
