#!/usr/bin/env bash
# End-to-end smoke test for the Longevity Copilot MCP server (v1.1).
# Usage: ./test_harness.sh [BASE_URL] [BEARER_TOKEN]

set -u
BASE=${1:-http://127.0.0.1:8080}
TOKEN=${2:-}
PASS=0
FAIL=0
AUTH_HEADER=""
if [ -n "$TOKEN" ]; then AUTH_HEADER="-H Authorization:Bearer\ $TOKEN"; fi

pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# Extract the inner text payload from an MCP tools/call response.
extract_text() {
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result']['content'][0]['text'])" 2>/dev/null
}

# POST JSON to /mcp; if TOKEN set, include it.
mcp_post() {
  local body=$1
  if [ -n "$TOKEN" ]; then
    curl -fsS -X POST "$BASE/mcp" \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer $TOKEN" \
      -d "$body"
  else
    curl -fsS -X POST "$BASE/mcp" \
      -H 'Content-Type: application/json' \
      -d "$body"
  fi
}

echo "Testing Longevity Copilot MCP at $BASE"
echo "Auth: $( [ -n "$TOKEN" ] && echo "Bearer ${TOKEN:0:4}***" || echo "none" )"
echo "=============================================="

# 1. Health
echo "[01] /healthz"
HEALTH=$(curl -fsS "$BASE/healthz")
if echo "$HEALTH" | grep -q '"status":"ok"'; then pass "healthz returns ok"; else fail "healthz"; fi

# 2. Health includes dependency probe
echo "[02] /healthz dependency probe"
if echo "$HEALTH" | grep -q '"hapi_fhir"'; then pass "healthz includes hapi_fhir status"; else fail "healthz deps"; fi

# 3. Root
echo "[03] /"
if curl -fsS "$BASE/" | grep -q 'longevity-copilot-mcp\|Longevity Copilot MCP'; then pass "root advertises service"; else fail "root"; fi

# 4. CORS preflight
echo "[04] CORS preflight"
CORS=$(curl -s -o /dev/null -w "%{http_code}" -X OPTIONS "$BASE/mcp" -H 'Access-Control-Request-Method: POST' -H 'Origin: https://app.promptopinion.ai')
if [ "$CORS" = "204" ]; then pass "OPTIONS returns 204"; else fail "OPTIONS got $CORS"; fi

# 5. initialize
echo "[05] MCP initialize"
INIT=$(mcp_post '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"harness","version":"1.0"}},"id":1}')
if echo "$INIT" | grep -q '"protocolVersion"'; then pass "initialize returned protocolVersion"; else fail "initialize"; fi

# 6. tools/list count
echo "[06] tools/list count >= 12"
TOOLS=$(mcp_post '{"jsonrpc":"2.0","method":"tools/list","id":2}')
COUNT=$(echo "$TOOLS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null)
if [ "$COUNT" -ge 12 ] 2>/dev/null; then pass "tools/list returned $COUNT tools"; else fail "tools/list (got: $COUNT)"; fi

# 7. tools/list includes calculators
echo "[07] calculators present"
if echo "$TOOLS" | grep -q 'calc_homa_ir' && echo "$TOOLS" | grep -q 'calc_egfr_ckdepi_2021' && echo "$TOOLS" | grep -q 'calc_ascvd_10yr'; then pass "all three calculators listed"; else fail "calculators missing"; fi

# 8. Tyrone labs (synthetic) include LOINC
echo "[08] get_patient_labs Tyrone synthetic"
LABS=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_patient_labs","arguments":{}},"id":3}' | extract_text)
if echo "$LABS" | grep -q '"loinc": "4548-4"'; then pass "Tyrone labs contain LOINC 4548-4 (HbA1c)"; else fail "Tyrone labs"; fi

# 9. Tyrone labs include fasting insulin (needed for HOMA-IR)
echo "[09] Tyrone labs include fasting insulin"
if echo "$LABS" | grep -q '"loinc": "1554-5"'; then pass "Tyrone labs include LOINC 1554-5 (fasting insulin)"; else fail "fasting insulin missing"; fi

# 10. normalize_biomarker covers expanded vendor map
echo "[10] normalize_biomarker labcorp:HEMOGLOBIN_A1C"
NORM=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"normalize_biomarker","arguments":{"vendor":"labcorp","vendor_code":"HEMOGLOBIN_A1C"}},"id":4}' | extract_text)
if echo "$NORM" | grep -q '"loinc": "4548-4"'; then pass "labcorp:HEMOGLOBIN_A1C maps to LOINC 4548-4"; else fail "normalize"; fi

# 11. normalize_biomarker handles new code (Lp(a))
echo "[11] normalize_biomarker quest:LIPOPROTEIN_A"
NORM2=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"normalize_biomarker","arguments":{"vendor":"quest","vendor_code":"LIPOPROTEIN_A"}},"id":5}' | extract_text)
if echo "$NORM2" | grep -q '"loinc": "10835-7"'; then pass "quest:LIPOPROTEIN_A maps to LOINC 10835-7"; else fail "normalize Lp(a)"; fi

# 12. Live HAPI demographics
echo "[12] get_patient_demographics live HAPI"
HAPI_ID=$(curl -fsS "https://hapi.fhir.org/baseR4/Patient?_count=1&_format=json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['entry'][0]['resource']['id'])" 2>/dev/null)
if [ -n "${HAPI_ID:-}" ]; then
  DEMOG=$(mcp_post "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"get_patient_demographics\",\"arguments\":{\"patient_id\":\"$HAPI_ID\"}},\"id\":6}" | extract_text)
  if echo "$DEMOG" | grep -q '"source": "hapi-fhir-r4"'; then pass "live HAPI demographics retrieved for $HAPI_ID"; else fail "live HAPI demographics"; fi
else
  fail "could not find a HAPI patient_id"
fi

# 13. Genomics
echo "[13] get_patient_genomics Tyrone"
GEN=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_patient_genomics","arguments":{}},"id":7}' | extract_text)
if echo "$GEN" | grep -q '"rsid": "rs4680"'; then pass "Tyrone genomics include COMT rs4680"; else fail "genomics"; fi

# 14. Wearables
echo "[14] get_wearable_snapshot Tyrone"
W=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_wearable_snapshot","arguments":{"window":"last_7_days"}},"id":8}' | extract_text)
if echo "$W" | grep -q '"hrv_rmssd_ms_mean"'; then pass "Tyrone wearable snapshot includes HRV"; else fail "wearables"; fi

# 15. calc_homa_ir
echo "[15] calc_homa_ir"
HOMA=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"calc_homa_ir","arguments":{"fasting_insulin_uIU_mL":7.2,"fasting_glucose_mg_dL":88}},"id":9}' | extract_text)
if echo "$HOMA" | grep -q '"homa_ir": 1.56'; then pass "HOMA-IR(7.2, 88) = 1.56"; else fail "HOMA-IR got: $(echo "$HOMA" | head -c 200)"; fi

# 16. calc_egfr_ckdepi_2021
echo "[16] calc_egfr_ckdepi_2021"
EGFR=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"calc_egfr_ckdepi_2021","arguments":{"creatinine_mg_dL":1.05,"age_years":26,"sex":"male"}},"id":10}' | extract_text)
if echo "$EGFR" | grep -q '"ckd_stage"' && echo "$EGFR" | grep -q '"egfr_mL_min_1_73m2"'; then pass "eGFR returned stage + value"; else fail "eGFR"; fi

# 17. calc_ascvd_10yr
echo "[17] calc_ascvd_10yr"
ASCVD=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"calc_ascvd_10yr","arguments":{"age_years":55,"sex":"male","race":"white","total_cholesterol_mg_dL":213,"hdl_mg_dL":50,"sbp_mmHg":120,"treated_for_hypertension":false,"diabetes":false,"smoker":false}},"id":11}' | extract_text)
if echo "$ASCVD" | grep -q '"ascvd_10yr_risk_pct"'; then pass "ASCVD risk calculated"; else fail "ASCVD"; fi

# 18. rxnav_interactions for Metformin
echo "[18] rxnav_interactions resolves Metformin"
RX=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"rxnav_interactions","arguments":{"drug_name":"Metformin"}},"id":12}' | extract_text)
if echo "$RX" | grep -qE '"rxcui": ?"6809"|"rxcui": ?6809'; then pass "Metformin resolves to RxCUI 6809"; else fail "RxNav metformin"; fi

# 19. Unknown tool returns JSON-RPC error
echo "[19] unknown tool returns error"
BAD=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"this_does_not_exist","arguments":{}},"id":13}')
if echo "$BAD" | grep -q '"error"'; then pass "unknown tool returns error"; else fail "unknown tool should error"; fi

# 20. Bad input is handled
echo "[20] HOMA-IR with missing inputs returns error"
BADIN=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"calc_homa_ir","arguments":{}},"id":14}' | extract_text)
if echo "$BADIN" | grep -q '"error"'; then pass "HOMA-IR rejects missing inputs"; else fail "HOMA-IR should reject empty args"; fi

# 21. Audit log captured the work
echo "[21] audit_tail returns events"
AT=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"audit_tail","arguments":{"n":10}},"id":15}' | extract_text)
if echo "$AT" | grep -q '"tool.call.ok"'; then pass "audit log captured tool.call.ok"; else fail "audit log"; fi

# 22. Auth: if TOKEN was set, an unauth call should be rejected
if [ -n "$TOKEN" ]; then
  echo "[22] unauth call rejected"
  UA=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/mcp" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"tools/list","id":99}')
  if [ "$UA" = "401" ]; then pass "missing bearer returns 401"; else fail "expected 401 got $UA"; fi
fi

# 23. PDF generation
echo "[23] generate_clinical_pdf"
PDF_JSON=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"generate_clinical_pdf","arguments":{"headline":"Test brief","patient_label":"Tyrone S.","findings":[{"name":"HbA1c","value":5.4,"unit":"%","source":"Quest"}],"plan":["Recheck in 90 days"]}},"id":16}' | extract_text)
REPORT_ID=$(echo "$PDF_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('report_id',''))" 2>/dev/null)
if [ -n "$REPORT_ID" ]; then pass "PDF generated, report_id=$REPORT_ID"; else fail "PDF generation"; fi

# 24. PDF served at /reports/{id}
if [ -n "$REPORT_ID" ]; then
  echo "[24] GET /reports/$REPORT_ID returns PDF"
  CT=$(curl -s -o /tmp/test.pdf -w "%{content_type}" "$BASE/reports/$REPORT_ID")
  if [ "$CT" = "application/pdf" ] && head -c 4 /tmp/test.pdf 2>/dev/null | grep -q '%PDF'; then
    pass "PDF served correctly (Content-Type=$CT)"
  else
    fail "PDF endpoint (CT=$CT)"
  fi
fi

# 25. Agent card
echo "[25] /.well-known/agent.json"
AC=$(curl -fsS "$BASE/.well-known/agent.json")
if echo "$AC" | grep -q '"Longevity Copilot MCP"' && echo "$AC" | grep -q '"skills"'; then pass "agent card valid"; else fail "agent card"; fi

# 26. Catalog page
echo "[26] /catalog"
CG=$(curl -fsS "$BASE/catalog")
if echo "$CG" | grep -q 'MCP - Tool Catalog'; then pass "catalog page served"; else fail "catalog page"; fi

# 27. list_supported_sources
echo "[27] tools/call list_supported_sources"
LSS=$(mcp_post '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_supported_sources","arguments":{}},"id":17}' | extract_text)
if echo "$LSS" | grep -q '"lab_vendors"' && echo "$LSS" | grep -q '"wearables"' && echo "$LSS" | grep -q '"ehr_sources"'; then pass "list_supported_sources returns all categories"; else fail "list_supported_sources"; fi

echo "=============================================="
echo "RESULTS: $PASS passed, $FAIL failed"
exit $FAIL
