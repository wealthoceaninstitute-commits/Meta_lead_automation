"""
Diagnostic helper: shows EXACTLY what the Meta Graph API returns at each step
of the ad_id -> adset -> campaign chain, without swallowing any errors.

Use this to answer "what are we actually receiving from Graph API?" empirically.

Wire it up in main.py:

    from app.debug_graph import diagnose_lead_chain

    @app.get("/debug/graph-lead")
    def debug_graph_lead(
        leadgen_id: str = "",
        ad_id: str = "",
        user: str = Depends(require_user),
    ):
        return diagnose_lead_chain(leadgen_id=leadgen_id, ad_id=ad_id)

Then call (with your JWT):
    GET /debug/graph-lead?leadgen_id=<a real leadgen id>
or
    GET /debug/graph-lead?ad_id=<a real ad id>
"""

import os
from typing import Any, Dict, List, Optional

import requests

from app.config import settings

META_GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")


def _token() -> str:
    # Mirror whatever the app actually uses, in priority order.
    return (
        os.getenv("META_PAGE_ACCESS_TOKEN")
        or os.getenv("META_ACCESS_TOKEN")
        or settings.meta_page_access_token
        or settings.meta_access_token
        or ""
    )


def _raw_get(object_id: str, fields: str, timeout: int = 20) -> Dict[str, Any]:
    """Do one Graph GET and return a full record of what came back.

    Never raises. Always returns status + parsed body (or error) so the caller
    can see permission errors, missing fields, etc.
    """
    token = _token()
    result: Dict[str, Any] = {
        "object_id": object_id,
        "requested_fields": fields,
        "url": f"https://graph.facebook.com/{META_GRAPH_VERSION}/{object_id}",
    }
    if not token:
        result["error"] = "NO TOKEN — neither META_PAGE_ACCESS_TOKEN nor META_ACCESS_TOKEN is set"
        return result
    if not object_id:
        result["error"] = "empty object_id — skipped"
        return result

    try:
        resp = requests.get(
            result["url"],
            params={"access_token": token, "fields": fields},
            timeout=timeout,
        )
        result["http_status"] = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {"_non_json_text": resp.text[:2000]}
        result["response"] = body

        # Surface Meta's error block clearly if present.
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            result["meta_error"] = {
                "message": err.get("message"),
                "type": err.get("type"),
                "code": err.get("code"),
                "error_subcode": err.get("error_subcode"),
                "fbtrace_id": err.get("fbtrace_id"),
            }
    except Exception as exc:
        result["exception"] = f"{type(exc).__name__}: {exc}"

    return result


def _token_introspection() -> Dict[str, Any]:
    """Ask Graph what this token actually is and what scopes it has."""
    token = _token()
    if not token:
        return {"error": "no token configured"}
    try:
        # /me tells us whether it's a Page token (category present) or a User token.
        me = requests.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/me",
            params={"access_token": token, "fields": "id,name,category,tasks"},
            timeout=15,
        )
        me_body = me.json()

        # /me/permissions works for user tokens; lists granted scopes.
        perms = requests.get(
            f"https://graph.facebook.com/{META_GRAPH_VERSION}/me/permissions",
            params={"access_token": token},
            timeout=15,
        )
        perms_body = perms.json()

        granted: List[str] = []
        if isinstance(perms_body, dict):
            for row in perms_body.get("data", []) or []:
                if row.get("status") == "granted" and row.get("permission"):
                    granted.append(row["permission"])

        looks_like_page = isinstance(me_body, dict) and "category" in me_body
        has_ads_read = "ads_read" in granted or "ads_management" in granted

        return {
            "me": me_body,
            "granted_permissions": granted or "(none returned — typical for a Page token)",
            "looks_like_page_token": looks_like_page,
            "has_ads_read_or_management": has_ads_read,
            "verdict": (
                "This looks like a PAGE token. Page tokens can read leads but "
                "usually CANNOT read ad/adset/campaign objects — that needs "
                "ads_read on a User or System-User token."
                if looks_like_page and not has_ads_read
                else "Token appears to have ads read access — campaign lookup should work."
                if has_ads_read
                else "Could not conclusively classify the token; inspect 'me' above."
            ),
        }
    except Exception as exc:
        return {"exception": f"{type(exc).__name__}: {exc}"}


def diagnose_lead_chain(leadgen_id: str = "", ad_id: str = "") -> Dict[str, Any]:
    """Walk lead -> ad -> adset -> campaign and return every raw response."""
    steps: Dict[str, Any] = {}
    resolved_ad_id: Optional[str] = ad_id or None

    steps["0_token_introspection"] = _token_introspection()

    # --- Step 1: the Lead object ------------------------------------------
    if leadgen_id:
        # Exactly what the app requests today:
        steps["1_lead_object__app_fields"] = _raw_get(
            leadgen_id,
            "id,created_time,field_data,ad_id,form_id,is_organic,platform",
        )
        # Also try asking the Lead directly for campaign fields, to PROVE the
        # Lead object does not carry campaign_id/adset_id/campaign_name.
        steps["1b_lead_object__campaign_fields_probe"] = _raw_get(
            leadgen_id,
            "id,ad_id,adgroup_id,campaign_id,campaign_name,adset_id,adset_name,ad_name,form_name",
        )
        lead_resp = steps["1_lead_object__app_fields"].get("response", {})
        if isinstance(lead_resp, dict):
            resolved_ad_id = resolved_ad_id or lead_resp.get("ad_id")

    # --- Step 2: the Ad object --------------------------------------------
    if resolved_ad_id:
        steps["2_ad_object"] = _raw_get(
            resolved_ad_id,
            "id,name,adset_id,adset{id,name,campaign_id},campaign_id,campaign{id,name}",
        )
        ad_resp = steps["2_ad_object"].get("response", {})
        adset_id = None
        campaign_id = None
        if isinstance(ad_resp, dict):
            adset_id = ad_resp.get("adset_id") or (ad_resp.get("adset") or {}).get("id")
            campaign_id = ad_resp.get("campaign_id") or (ad_resp.get("campaign") or {}).get("id")

        # --- Step 3: the Ad Set object ------------------------------------
        if adset_id:
            steps["3_adset_object"] = _raw_get(
                str(adset_id), "id,name,campaign_id,campaign{id,name}"
            )
            if not campaign_id:
                adset_resp = steps["3_adset_object"].get("response", {})
                if isinstance(adset_resp, dict):
                    campaign_id = adset_resp.get("campaign_id") or (
                        adset_resp.get("campaign") or {}
                    ).get("id")

        # --- Step 4: the Campaign object ----------------------------------
        if campaign_id:
            steps["4_campaign_object"] = _raw_get(str(campaign_id), "id,name,objective")
    else:
        steps["2_ad_object"] = {"skipped": "no ad_id available from lead or query param"}

    # --- Summary verdict --------------------------------------------------
    ad_ok = isinstance(steps.get("2_ad_object", {}).get("response"), dict) and (
        "error" not in steps.get("2_ad_object", {}).get("response", {})
    )
    campaign_name = None
    camp = steps.get("4_campaign_object", {})
    if isinstance(camp.get("response"), dict):
        campaign_name = camp["response"].get("name")

    steps["SUMMARY"] = {
        "resolved_ad_id": resolved_ad_id,
        "ad_object_readable": ad_ok,
        "campaign_name_resolved": campaign_name,
        "diagnosis": (
            f"Campaign name resolves fine: '{campaign_name}'. "
            "If the CRM still shows an ID, the bug is in enrichment/storage, not permissions."
            if campaign_name
            else "Campaign name could NOT be resolved. Check the meta_error blocks above — "
            "code 200/10/803 or 'ads_read' messages mean the token lacks ad permissions "
            "(the usual root cause for adset IDs showing instead of campaign names)."
        ),
    }

    return steps
