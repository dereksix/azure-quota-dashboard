"""
Azure Quota Dashboard — backend.

Self-hosted dashboard that hits Azure ARM directly for compute quota usage,
plus Resource Graph for the per-VM-family OD vs Spot breakdown.

  Token scope: https://management.azure.com/.default
  Auth:        local `az` CLI, MSAL device-code (Azure CLI well-known clientId), or paste-token
  Endpoints:
    GET /subscriptions?api-version=2022-12-01                                 → sub list
    GET /subscriptions/{sub}/providers/Microsoft.Compute
        /locations/{region}/usages?api-version=2024-07-01                     → quota usage
    POST /providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01  → per-family OD vs Spot

  Compute usages response shape:
    { value: [{ name:{value, localizedValue}, currentValue, limit, unit }, ...] }
  We adapt that into { SkuUsages: [{VmFamily, CurrentQuota, CurrentUsage}] }
  internally so the frontend (KPIs, deltas, trend, groups) is data-shape agnostic.

  Requires no app registration: uses the public Azure CLI clientId
  (04b07795-8ddb-461a-bbee-02f9e1bf7b46), which is pre-consented for ARM
  in every Entra tenant.

Persistence: data/jobs/{id}.json — one file per snapshot.

Cost: zero. ARM Compute usages, Resource Graph, and /subscriptions are all
free. The dashboard runs locally on the user's machine.
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import msal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT       = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT / "data"
FRONTEND   = ROOT / "frontend"
JOBS_DIR   = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

ARM_BASE   = "https://management.azure.com"
ARM_SCOPE  = "https://management.azure.com/.default"
# Public well-known clientId of the Azure CLI — pre-consented for ARM
# in every Entra tenant, so users don't need to register their own app.
CLIENT_ID  = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
# We default to "organizations" so any work/school account in any tenant works.
AUTHORITY  = "https://login.microsoftonline.com/organizations"

# Tunables
DEFAULT_REGION   = "eastus"
DEFAULT_SERVICE  = "compute"
DEFAULT_THROTTLE = 10
PER_CALL_TIMEOUT = 60   # seconds — ARM is fast (<2s typical)


# ---------------------------------------------------------------------------
# MSAL token cache (single-user desktop app)
# ---------------------------------------------------------------------------
_TOKEN_CACHE_PATH = ROOT / ".msal_cache.bin"
_msal_app: msal.PublicClientApplication | None = None
_cache: msal.SerializableTokenCache | None = None
_token: str | None = None
_token_expires_at: float = 0.0


def _msal() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    global _msal_app, _cache
    if _msal_app is None:
        _cache = msal.SerializableTokenCache()
        if _TOKEN_CACHE_PATH.exists():
            try:
                _cache.deserialize(_TOKEN_CACHE_PATH.read_text())
            except Exception:
                pass
        _msal_app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=_cache)
    return _msal_app, _cache


def _save_cache() -> None:
    _, c = _msal()
    if c.has_state_changed:
        _TOKEN_CACHE_PATH.write_text(c.serialize())


def _set_token(access_token: str, expires_in: int = 3600) -> None:
    global _token, _token_expires_at
    _token = access_token
    _token_expires_at = time.time() + int(expires_in)


def get_token_silent() -> str | None:
    """Return cached token if valid, else try MSAL silent refresh from on-disk cache."""
    global _token, _token_expires_at
    if _token and _token_expires_at > time.time() + 60:
        return _token

    env = os.environ.get("AZURE_ACCESS_TOKEN")
    if env:
        _set_token(env)
        return _token

    app, _ = _msal()
    for acct in app.get_accounts():
        result = app.acquire_token_silent([ARM_SCOPE], account=acct)
        if result and "access_token" in result:
            _set_token(result["access_token"], result.get("expires_in", 3600))
            _save_cache()
            return _token
    return None


def get_token() -> str:
    t = get_token_silent()
    if not t:
        raise RuntimeError("Not signed in — use the Sign-in panel to acquire a token.")
    return t


# ---- Token acquisition strategies -----------------------------------------

def acquire_token_from_az_cli() -> dict:
    """Use the local 'az' CLI to mint an ARM-scoped token. Best for users already signed into Azure CLI."""
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        raise RuntimeError("Azure CLI not found on PATH.")
    try:
        out = subprocess.run(
            [az, "account", "get-access-token", "--resource", ARM_BASE, "-o", "json"],
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("az CLI call timed out.")
    if out.returncode != 0:
        raise RuntimeError(f"az failed: {out.stderr.strip()[:400]}")
    j = json.loads(out.stdout)
    tok = j.get("accessToken")
    if not tok:
        raise RuntimeError("az returned no accessToken.")
    _set_token(tok, expires_in=55 * 60)
    return {"ok": True, "user": j.get("subscription") and "(via az CLI)" or "(via az CLI)"}


# ---- Device code flow -----------------------------------------------------
_device_flow_state: dict | None = None
_device_flow_lock = threading.Lock()


def device_code_start() -> dict:
    global _device_flow_state
    app, _ = _msal()
    flow = app.initiate_device_flow(scopes=[ARM_SCOPE])
    if "user_code" not in flow:
        raise RuntimeError(flow.get("error_description") or "Device flow not enabled for this app.")
    with _device_flow_lock:
        _device_flow_state = {"flow": flow, "started": time.time(), "status": "pending",
                                "error": None, "user": None}

    def _poll():
        global _device_flow_state
        result = app.acquire_token_by_device_flow(flow)
        with _device_flow_lock:
            if "access_token" in result:
                _set_token(result["access_token"], result.get("expires_in", 3600))
                _save_cache()
                accts = app.get_accounts()
                _device_flow_state.update({"status": "done",
                                            "user": accts[0]["username"] if accts else None})
            else:
                _device_flow_state.update({"status": "error",
                                            "error": result.get("error_description") or str(result)})

    threading.Thread(target=_poll, daemon=True).start()
    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"],
             "message": flow.get("message"), "expires_in": flow.get("expires_in")}


def device_code_status() -> dict:
    with _device_flow_lock:
        if not _device_flow_state:
            return {"status": "idle"}
        return {"status": _device_flow_state["status"],
                "error":  _device_flow_state.get("error"),
                "user":   _device_flow_state.get("user")}


# ---------------------------------------------------------------------------
# Sub list — live from ARM (no CSV)
# ---------------------------------------------------------------------------
async def fetch_subscriptions(client: httpx.AsyncClient, token: str) -> list[dict]:
    """List every subscription the signed-in user can read."""
    out: list[dict] = []
    url = f"{ARM_BASE}/subscriptions?api-version=2022-12-01"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    while url:
        r = await client.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        body = r.json()
        for s in body.get("value", []):
            sid = (s.get("subscriptionId") or "").strip().lower()
            name = (s.get("displayName") or "").strip()
            state = s.get("state", "")
            if sid and state == "Enabled":
                out.append({"sub_guid": sid, "sub_name": name, "tenant_id": s.get("tenantId", "")})
        url = body.get("nextLink") or ""
    return out


# ---------------------------------------------------------------------------
# ARM Compute usages caller — produces a uniform internal SkuUsages shape
# ---------------------------------------------------------------------------
async def fetch_quota(client: httpx.AsyncClient, sub_guid: str, region: str, service: str,
                       token: str) -> dict:
    """Fetch per-family quota usage for a single sub+region from ARM Compute API.

    Adapts Compute usages response into { SubscriptionId, Cloud, SkuUsages:[...] }
    shape used internally by the dashboard.
    """
    # `service` is currently always 'compute' — kept as a parameter for forward
    # compatibility (storage, network, etc. expose the same usages pattern).
    url = (f"{ARM_BASE}/subscriptions/{sub_guid}/providers/Microsoft.Compute/"
           f"locations/{_url_encode(region)}/usages?api-version=2024-07-01")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    t0 = time.perf_counter()
    short_sub = sub_guid[:8]
    try:
        r = await client.get(url, headers=headers, timeout=PER_CALL_TIMEOUT)
        elapsed = time.perf_counter() - t0
        body_snip = (r.text or "")[:500].replace("\n", " ")
        if r.status_code != 200:
            print(f"[arm] sub={short_sub} HTTP {r.status_code} in {elapsed:5.1f}s  body={body_snip}",
                  flush=True)
            return {"sub_guid": sub_guid, "status": "ERROR", "http": r.status_code,
                    "error": r.text[:500], "elapsed": round(elapsed, 1)}
        try:
            raw = r.json()
        except Exception as je:
            return {"sub_guid": sub_guid, "status": "ERROR", "http": 200,
                    "error": f"non-JSON: {je}", "elapsed": round(elapsed, 1)}
        # Adapt to internal SkuUsages-shape payload
        sku_usages = []
        cores_row = None
        seen_star = False
        for v in raw.get("value", []):
            name_obj = v.get("name") or {}
            family = name_obj.get("value") if isinstance(name_obj, dict) else str(name_obj)
            if not family:
                continue
            entry = {
                "VmFamily":     family,
                "CurrentQuota": int(v.get("limit") or 0),
                "CurrentUsage": int(v.get("currentValue") or 0),
            }
            sku_usages.append(entry)
            if family == "cores":
                cores_row = entry
            elif family == "*":
                seen_star = True
        # ARM exposes the regional OD cap as `cores`; legacy callers expect `*`.
        # Emit an alias so the frontend's regional-rollup logic works unchanged.
        if cores_row and not seen_star:
            sku_usages.append({
                "VmFamily":     "*",
                "CurrentQuota": cores_row["CurrentQuota"],
                "CurrentUsage": cores_row["CurrentUsage"],
            })
        adapted = {
            "SubscriptionId":      sub_guid,
            "Cloud":               "Public",
            "SkuUsages":           sku_usages,
            "SkuRestrictions":     [],
            "SkuRestrictionsZonal":[],
            "RegionalVmQuota":     0,
        }
        sku_count = len(sku_usages)
        tag = "OK" if sku_count else "EMPTY"
        print(f"[arm] sub={short_sub} {tag:5} {sku_count:>3} SKUs in {elapsed:5.1f}s", flush=True)
        return {"sub_guid": sub_guid, "status": "OK", "http": 200,
                "data": adapted, "elapsed": round(elapsed, 1)}
    except httpx.TimeoutException:
        elapsed = time.perf_counter() - t0
        print(f"[arm] sub={short_sub} TIMEOUT after {elapsed:5.1f}s", flush=True)
        return {"sub_guid": sub_guid, "status": "ERROR", "http": 0,
                "error": "request timeout", "elapsed": round(elapsed, 1)}
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f"[arm] sub={short_sub} EXCEPTION {type(e).__name__}: {e}  ({elapsed:.1f}s)",
              flush=True)
        return {"sub_guid": sub_guid, "status": "ERROR", "http": 0,
                "error": f"{type(e).__name__}: {e}"[:500], "elapsed": round(elapsed, 1)}


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---------------------------------------------------------------------------
# Resource Graph — per-family OD vs Spot core usage
# ---------------------------------------------------------------------------
async def fetch_vm_priority_breakdown(client: httpx.AsyncClient, sub_guids: list[str],
                                        region: str, token: str) -> dict:
    """Run a Resource Graph query that returns per-family OD vs Spot vCPU usage.

    Returns:
      {
        "rows": [{"sub_guid","sub_name","family","priority","vms","cores"}, ...],
        "totals": {"od_cores": N, "spot_cores": M, "od_vms":..., "spot_vms":...}
      }

    Implementation notes:
      - We summarize VMs and VMSS instances by sku.
      - Cores per VM are looked up via a join against the
        Microsoft.Compute/skus catalog (also exposed through Resource Graph).
      - Family is the Compute SKU "family" property (same string that appears
        in the quota usages, e.g. standardDasv6Family).
    """
    if not sub_guids:
        return {"rows": [], "totals": {"od_cores": 0, "spot_cores": 0,
                                          "od_vms": 0, "spot_vms": 0}}
    # The vmSize string from VMs (e.g. "Standard_D8s_v5") joins with the
    # ResourceContainers SKU table on `name`. That table exposes both
    # `family` (e.g. standardDSv5Family) and `capabilities` array which
    # contains "vCPUs". Resource Graph KQL syntax follows.
    query = """
Resources
| where type =~ 'microsoft.compute/virtualmachines'
| where tolower(location) == tolower('{region}')
| extend size = tostring(properties.hardwareProfile.vmSize),
         priority = iff(isempty(tostring(properties.priority)) or tostring(properties.priority) =~ 'Regular', 'OD', 'Spot')
| project subscriptionId, vmName=name, size, priority
| join kind=leftouter (
    ResourceContainers
    | where type =~ 'microsoft.resources/subscriptions/resources'
) on subscriptionId
| summarize vms = count() by subscriptionId, size, priority
""".strip().format(region=region)
    # Actually Resource Container lookups for SKUs aren't in the Resources table the way
    # we want. Use a simpler approach: query VMs grouped by size/priority, then resolve
    # core counts via the Compute SKUs API once and multiply.
    query = (
        "Resources "
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| extend size = tostring(properties.hardwareProfile.vmSize), "
        "         priority = iff(isempty(tostring(properties.priority)) or "
        "                        tostring(properties.priority) =~ 'Regular', 'OD', 'Spot') "
        "| summarize vms = count() by subscriptionId, location, size, priority"
    )

    payload = {
        "subscriptions": sub_guids,
        "query": query,
        "options": {"resultFormat": "table", "$top": 1000},
    }
    headers = {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json", "Accept": "application/json"}
    url = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
    t0 = time.perf_counter()
    try:
        r = await client.post(url, headers=headers, json=payload, timeout=60)
        elapsed = time.perf_counter() - t0
        if r.status_code != 200:
            print(f"[rg] HTTP {r.status_code} in {elapsed:.1f}s body={r.text[:300]}", flush=True)
            return {"rows": [], "totals": {"od_cores": 0, "spot_cores": 0,
                                              "od_vms": 0, "spot_vms": 0},
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        body = r.json()
    except Exception as e:
        return {"rows": [], "totals": {"od_cores": 0, "spot_cores": 0,
                                          "od_vms": 0, "spot_vms": 0},
                "error": f"{type(e).__name__}: {e}"}

    cols = [c["name"] for c in body.get("data", {}).get("columns", [])]
    rg_rows = body.get("data", {}).get("rows", [])

    region_lc = (region or "").lower()
    out_rows: list[dict] = []
    totals = {"od_cores": 0, "spot_cores": 0, "od_vms": 0, "spot_vms": 0}
    # Other regions: VMs in the selected subs that AREN'T in the requested region.
    # Surfaced separately so users see "you have spot elsewhere" without
    # contaminating the in-scope inventory table.
    other_by_region: dict[str, dict] = {}
    sku_meta_cache: dict[str, dict] = {}
    for row in rg_rows:
        d = dict(zip(cols, row))
        sub_id   = (d.get("subscriptionId") or "").lower()
        location = (d.get("location") or "").lower()
        size     = d.get("size") or ""
        priority = d.get("priority") or "OD"
        vms      = int(d.get("vms") or 0)
        if location and location not in sku_meta_cache:
            try:
                sku_meta_cache[location] = await fetch_compute_sku_meta(
                    client, sub_guids[0], location, token)
            except Exception:
                sku_meta_cache[location] = {}
        meta = sku_meta_cache.get(location, {}).get(size.lower(), {})
        cores  = vms * int(meta.get("vCPUs") or 0)
        family = meta.get("family") or "(unknown)"
        if region_lc and location != region_lc:
            bucket = other_by_region.setdefault(location, {
                "location": location,
                "od_vms": 0, "od_cores": 0, "spot_vms": 0, "spot_cores": 0,
            })
            if priority == "Spot":
                bucket["spot_vms"]   += vms
                bucket["spot_cores"] += cores
            else:
                bucket["od_vms"]   += vms
                bucket["od_cores"] += cores
            continue
        out_rows.append({
            "sub_guid": sub_id, "location": location, "size": size,
            "family": family, "priority": priority,
            "vms": vms, "cores": cores,
        })
        if priority == "Spot":
            totals["spot_cores"] += cores
            totals["spot_vms"]   += vms
        else:
            totals["od_cores"]   += cores
            totals["od_vms"]     += vms

    return {
        "rows": out_rows,
        "totals": totals,
        "region": region_lc,
        "other_regions": sorted(
            other_by_region.values(),
            key=lambda x: -(x["spot_cores"] + x["od_cores"]),
        ),
    }


_sku_meta_cache: dict[str, dict] = {}


async def fetch_compute_sku_meta(client: httpx.AsyncClient, sub_guid: str, region: str,
                                    token: str) -> dict:
    """Map vmSize → {family, vCPUs} for a region. Cached in-process."""
    cache_key = f"{sub_guid}|{region}".lower()
    if cache_key in _sku_meta_cache:
        return _sku_meta_cache[cache_key]
    url = (f"{ARM_BASE}/subscriptions/{sub_guid}/providers/Microsoft.Compute/skus"
           f"?api-version=2021-07-01&$filter=location eq '{region}'")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict[str, dict] = {}
    while url:
        r = await client.get(url, headers=headers, timeout=60)
        if r.status_code != 200:
            print(f"[skus] HTTP {r.status_code} body={r.text[:200]}", flush=True)
            break
        body = r.json()
        for s in body.get("value", []):
            if s.get("resourceType") != "virtualMachines":
                continue
            name = (s.get("name") or "").lower()
            family = s.get("family") or ""
            vcpus = 0
            for cap in s.get("capabilities") or []:
                if cap.get("name") == "vCPUs":
                    try:
                        vcpus = int(cap.get("value") or 0)
                    except Exception:
                        vcpus = 0
                    break
            out[name] = {"family": family, "vCPUs": vcpus}
        url = body.get("nextLink") or ""
    _sku_meta_cache[cache_key] = out
    return out


# ---------------------------------------------------------------------------
# Job manager (in-memory + on-disk persistence)
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, region: str, service: str, subs: list[dict], throttle: int):
        self.id        = uuid.uuid4().hex[:12]
        self.region    = region
        self.service   = service
        self.throttle  = throttle
        self.subs      = subs
        self.total     = len(subs)
        self.done      = 0
        self.results: list[dict] = []
        self.priority_breakdown: dict | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.status    = "running"
        self.error: str | None = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self._sub_index = {s["sub_guid"]: s for s in subs}

    def sub_name(self, guid: str) -> str:
        return self._sub_index.get(guid, {}).get("sub_name", "")

    def snapshot(self) -> dict:
        return {
            "id": self.id, "region": self.region, "service": self.service,
            "total": self.total, "done": self.done, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "throttle": self.throttle, "error": self.error,
        }


JOBS: dict[str, Job] = {}


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def persist_job(job: Job) -> None:
    payload = {
        "snapshot":           job.snapshot(),
        "subs":               job.subs,
        "results":            job.results,
        "priority_breakdown": job.priority_breakdown,
    }
    tmp = _job_path(job.id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(_job_path(job.id))


def load_job_from_disk(job_id: str) -> Job | None:
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[persist] failed to load {p.name}: {e}", flush=True)
        return None
    snap = payload["snapshot"]
    job = Job(region=snap["region"], service=snap["service"],
              subs=payload.get("subs", []), throttle=snap.get("throttle", DEFAULT_THROTTLE))
    job.id = snap["id"]
    job.results = payload.get("results", [])
    job.priority_breakdown = payload.get("priority_breakdown")
    job.done = len(job.results)
    job.total = snap.get("total", job.done)
    job.started_at = snap.get("started_at") or time.time()
    job.finished_at = snap.get("finished_at")
    job.status = snap.get("status", "done")
    return job


def get_job(job_id: str) -> Job | None:
    j = JOBS.get(job_id)
    if j:
        return j
    j = load_job_from_disk(job_id)
    if j:
        JOBS[job_id] = j
    return j


def list_persisted_jobs() -> list[dict]:
    out = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            snap = json.loads(p.read_text(encoding="utf-8")).get("snapshot", {})
            if snap:
                out.append(snap)
        except Exception:
            continue
    return out


async def run_job(job: Job) -> None:
    try:
        token = get_token()
    except Exception as e:
        print(f"[job {job.id}] AUTH FAIL: {e}", flush=True)
        job.status = "error"
        job.error = f"Auth failed: {e}"
        job.finished_at = time.time()
        await job.queue.put({"type": "done", "snapshot": job.snapshot()})
        return

    print(f"[job {job.id}] START region='{job.region}' service='{job.service}' "
           f"subs={job.total} throttle={job.throttle}", flush=True)

    sem = asyncio.Semaphore(job.throttle)
    limits = httpx.Limits(max_connections=job.throttle * 2, max_keepalive_connections=job.throttle)

    async with httpx.AsyncClient(http2=False, limits=limits) as client:
        async def one(sub: dict):
            async with sem:
                r = await fetch_quota(client, sub["sub_guid"], job.region, job.service, token)
                r["sub_name"] = sub["sub_name"]
                job.results.append(r)
                job.done += 1
                if job.done % 10 == 0 or job.done == job.total:
                    ok = sum(1 for x in job.results if x["status"] == "OK" and x.get("data", {}).get("SkuUsages"))
                    empty = sum(1 for x in job.results if x["status"] == "OK" and not x.get("data", {}).get("SkuUsages"))
                    err = sum(1 for x in job.results if x["status"] != "OK")
                    print(f"[job {job.id}] progress {job.done}/{job.total}  "
                           f"ok={ok} empty={empty} err={err}", flush=True)
                await job.queue.put({"type": "progress", "result": r,
                                     "done": job.done, "total": job.total})
        await asyncio.gather(*[one(s) for s in job.subs])

        # After all per-sub usages, run a single Resource Graph query for OD/Spot breakdown.
        try:
            ok_subs = [r["sub_guid"] for r in job.results if r["status"] == "OK"]
            if ok_subs:
                pb = await fetch_vm_priority_breakdown(client, ok_subs, job.region, token)
                # Backfill sub_name on each row for the UI.
                name_by_id = {s["sub_guid"]: s.get("sub_name", "") for s in job.subs}
                for row in pb.get("rows", []):
                    row["sub_name"] = name_by_id.get(row["sub_guid"], "")
                job.priority_breakdown = pb
                print(f"[job {job.id}] priority breakdown: "
                       f"OD {pb['totals']['od_cores']} cores / {pb['totals']['od_vms']} VMs | "
                       f"Spot {pb['totals']['spot_cores']} cores / {pb['totals']['spot_vms']} VMs",
                       flush=True)
        except Exception as e:
            print(f"[job {job.id}] priority breakdown FAIL: {e}", flush=True)

    ok = sum(1 for x in job.results if x["status"] == "OK" and x.get("data", {}).get("SkuUsages"))
    empty = sum(1 for x in job.results if x["status"] == "OK" and not x.get("data", {}).get("SkuUsages"))
    err = sum(1 for x in job.results if x["status"] != "OK")
    elapsed = time.time() - job.started_at
    print(f"[job {job.id}] DONE in {elapsed:.1f}s | ok={ok} empty={empty} err={err}", flush=True)
    job.status = "done"
    job.finished_at = time.time()
    try:
        persist_job(job)
        print(f"[job {job.id}] persisted to {_job_path(job.id).name}", flush=True)
    except Exception as e:
        print(f"[job {job.id}] PERSIST FAIL: {e}", flush=True)
    await job.queue.put({"type": "done", "snapshot": job.snapshot()})


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Azure Quota Dashboard")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html",
                         headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/api/subs")
async def api_subs() -> JSONResponse:
    """Return the full list of subscriptions the signed-in user can read."""
    try:
        token = get_token()
    except Exception as e:
        raise HTTPException(401, str(e))
    async with httpx.AsyncClient() as client:
        try:
            subs = await fetch_subscriptions(client, token)
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, e.response.text[:300])
    return JSONResponse(subs)


@app.get("/api/locations")
async def api_locations() -> JSONResponse:
    """Return Azure regions available to the signed-in user.

    Uses the first readable subscription as the scope (locations are tenant-wide,
    but the API is scoped to a sub). Returns physical (non-logical) regions only,
    sorted by display name.
    """
    try:
        token = get_token()
    except Exception as e:
        raise HTTPException(401, str(e))
    async with httpx.AsyncClient() as client:
        try:
            subs = await fetch_subscriptions(client, token)
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, e.response.text[:300])
        if not subs:
            return JSONResponse([])
        sub_guid = subs[0]["sub_guid"]
        url = f"{ARM_BASE}/subscriptions/{sub_guid}/locations?api-version=2022-12-01"
        try:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, e.response.text[:300])
        data = r.json().get("value", [])
        # Filter to physical Azure regions and surface a clean payload.
        out = []
        for loc in data:
            if loc.get("metadata", {}).get("regionType") != "Physical":
                continue
            out.append({
                "name": loc.get("name"),
                "display_name": loc.get("displayName"),
                "geography": loc.get("metadata", {}).get("geographyGroup"),
            })
        out.sort(key=lambda x: (x.get("geography") or "", x.get("display_name") or ""))
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# Microsoft.Quota Group Quotas (the real Azure construct, MG-scoped)
# ---------------------------------------------------------------------------
@app.get("/api/quota-groups")
async def api_quota_groups() -> JSONResponse:
    """Return real Microsoft.Quota Group Quotas the user can read.

    Walks management groups the signed-in user has access to, then enumerates
    `Microsoft.Quota/groupQuotas` under each. For each group, fetches member
    subscriptions and quota allocations.

    Empty list when the tenant has no quota groups configured (the common case).
    Surfaces a structured `{ "groups": [...], "errors": [...], "mgmt_groups": N }`.
    """
    try:
        token = get_token()
    except Exception as e:
        raise HTTPException(401, str(e))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    api_qg = "2025-03-15"        # Microsoft.Quota groupQuotas
    api_mg = "2020-05-01"        # management groups list
    out_groups: list[dict] = []
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: list management groups (Reader at MG scope required).
        url = f"{ARM_BASE}/providers/Microsoft.Management/managementGroups?api-version={api_mg}"
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            mgs = r.json().get("value", [])
        except httpx.HTTPStatusError as e:
            return JSONResponse({
                "groups": [],
                "errors": [f"List management groups: HTTP {e.response.status_code}: {e.response.text[:200]}"],
                "mgmt_groups": 0,
            })
        # Step 2: for each MG, enumerate groupQuotas.
        for mg in mgs:
            mg_id = mg.get("name") or ""
            mg_display = mg.get("properties", {}).get("displayName") or mg_id
            qg_url = (f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/"
                      f"{mg_id}/providers/Microsoft.Quota/groupQuotas?api-version={api_qg}")
            try:
                r = await client.get(qg_url, headers=headers)
                if r.status_code == 404:
                    continue        # No groups under this MG.
                r.raise_for_status()
                groups = r.json().get("value", [])
            except httpx.HTTPStatusError as e:
                # 403 is common (no Reader on this MG); silently skip those.
                if e.response.status_code not in (403, 401):
                    errors.append(f"MG {mg_id}: HTTP {e.response.status_code}: {e.response.text[:160]}")
                continue
            for g in groups:
                gname = g.get("name") or ""
                gprops = g.get("properties") or {}
                gdisplay = gprops.get("displayName") or gname
                # Pull member subscriptions.
                sub_ids: list[str] = []
                subs_url = (f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/"
                            f"{mg_id}/providers/Microsoft.Quota/groupQuotas/"
                            f"{gname}/subscriptions?api-version={api_qg}")
                try:
                    rs = await client.get(subs_url, headers=headers)
                    if rs.status_code == 200:
                        for s in rs.json().get("value", []):
                            sid = (s.get("name") or "").lower()
                            if sid:
                                sub_ids.append(sid)
                except Exception:
                    pass
                # Pull quota allocations.
                quotas: list[dict] = []
                qurl = (f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/"
                        f"{mg_id}/providers/Microsoft.Quota/groupQuotas/"
                        f"{gname}/quotas?api-version={api_qg}")
                try:
                    rq = await client.get(qurl, headers=headers)
                    if rq.status_code == 200:
                        for q in rq.json().get("value", []):
                            qp = q.get("properties") or {}
                            limit = qp.get("limit") or {}
                            quotas.append({
                                "name":         q.get("name"),
                                "resource":     qp.get("resourceName") or q.get("name"),
                                "limit":        limit.get("value") if isinstance(limit, dict) else limit,
                                "unit":         qp.get("unit"),
                                "comment":      qp.get("comment"),
                            })
                except Exception:
                    pass
                out_groups.append({
                    "mgmt_group_id":      mg_id,
                    "mgmt_group_display": mg_display,
                    "group_name":         gname,
                    "group_display":      gdisplay,
                    "subscriptions":      sub_ids,
                    "quotas":             quotas,
                })
    return JSONResponse({
        "groups": out_groups,
        "errors": errors,
        "mgmt_groups": len(mgs),
    })


@app.get("/api/auth/status")
async def api_auth_status() -> JSONResponse:
    get_token_silent()
    app_, _ = _msal()
    accs = app_.get_accounts()
    has_token = _token is not None and _token_expires_at > time.time()
    az_available = bool(shutil.which("az") or shutil.which("az.cmd"))
    return JSONResponse({
        "signed_in": has_token,
        "user": accs[0]["username"] if accs else None,
        "expires_at": _token_expires_at if has_token else None,
        "az_available": az_available,
    })


@app.post("/api/auth/az")
async def api_auth_az() -> JSONResponse:
    try:
        info = acquire_token_from_az_cli()
        return JSONResponse(info)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/auth/devicecode/start")
async def api_auth_devicecode_start() -> JSONResponse:
    try:
        info = device_code_start()
        return JSONResponse(info)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/auth/devicecode/status")
async def api_auth_devicecode_status() -> JSONResponse:
    return JSONResponse(device_code_status())


@app.post("/api/auth/paste")
async def api_auth_paste(req: Request) -> JSONResponse:
    body = await req.json()
    tok = (body.get("token") or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if not tok or tok.count(".") != 2:
        raise HTTPException(400, "That doesn't look like a JWT (expected 3 dot-separated segments).")
    import base64
    try:
        payload_b64 = tok.split(".")[1] + "=="
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode("utf-8", "ignore"))
        exp = int(payload.get("exp") or 0)
        ttl = max(60, exp - int(time.time())) if exp else 3600
        user = payload.get("upn") or payload.get("preferred_username") or payload.get("unique_name")
    except Exception:
        ttl = 3600
        user = None
    _set_token(tok, expires_in=ttl)
    return JSONResponse({"ok": True, "user": user, "expires_in": ttl})


@app.post("/api/auth/clear")
async def api_auth_clear() -> JSONResponse:
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0
    return JSONResponse({"ok": True})


@app.post("/api/job")
async def api_create_job(req: Request) -> JSONResponse:
    body = await req.json()
    region   = body.get("region",   DEFAULT_REGION)
    service  = body.get("service",  DEFAULT_SERVICE)
    throttle = int(body.get("throttle", DEFAULT_THROTTLE))
    subs_in  = body.get("subs")
    if subs_in is None:
        # No subs specified — fetch live from ARM.
        try:
            token = get_token()
        except Exception as e:
            raise HTTPException(401, str(e))
        async with httpx.AsyncClient() as client:
            subs = await fetch_subscriptions(client, token)
    else:
        subs = []
        for x in subs_in:
            if isinstance(x, str):
                subs.append({"sub_guid": x.strip().lower(), "sub_name": ""})
            elif isinstance(x, dict):
                subs.append({"sub_guid": x["sub_guid"].strip().lower(),
                             "sub_name": x.get("sub_name", "")})
    if not subs:
        raise HTTPException(400, "No subscriptions resolved.")

    job = Job(region=region, service=service, subs=subs, throttle=throttle)
    JOBS[job.id] = job
    asyncio.create_task(run_job(job))
    return JSONResponse({"job_id": job.id, **job.snapshot()})


@app.get("/api/jobs")
async def api_jobs_list() -> JSONResponse:
    return JSONResponse({"jobs": list_persisted_jobs()})


@app.get("/api/jobs/trend")
async def api_jobs_trend(subq: str = "", region: str = "") -> JSONResponse:
    """Time-series of total OD vs Spot quota and usage across all persisted jobs."""
    points: list[dict] = []
    sq = (subq or "").strip().lower()
    rq = (region or "").strip().lower()
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        snap = payload.get("snapshot") or {}
        if rq and (snap.get("region") or "").lower() != rq:
            continue
        results = payload.get("results", [])
        subs    = payload.get("subs", [])
        # Filter subs by subq if provided
        if sq:
            matched = {s["sub_guid"] for s in subs
                        if sq in (s.get("sub_name", "") or "").lower()
                            or sq in (s.get("sub_guid", "") or "").lower()}
        else:
            matched = {s["sub_guid"] for s in subs}
        od_q = od_u = sp_q = sp_u = 0
        for r in results:
            if r.get("status") != "OK" or r["sub_guid"] not in matched:
                continue
            for s in (r.get("data", {}).get("SkuUsages") or []):
                f = s.get("VmFamily")
                q = int(s.get("CurrentQuota") or 0)
                u = int(s.get("CurrentUsage") or 0)
                if f == "*" or f == "cores":
                    od_q += q; od_u += u
                elif f == "lowPriorityCores":
                    sp_q += q; sp_u += u
        points.append({
            "job_id":           snap.get("id"),
            "finished_at":      snap.get("finished_at"),
            "finished_at_iso":  None,
            "region":           snap.get("region"),
            "subs_total":       len(subs),
            "subs_matched":     len(matched),
            "od_quota": od_q, "od_used": od_u,
            "spot_quota": sp_q, "spot_used": sp_u,
        })
    return JSONResponse({"points": points})


@app.delete("/api/job/{job_id}")
async def api_job_delete(job_id: str) -> JSONResponse:
    JOBS.pop(job_id, None)
    p = _job_path(job_id)
    if p.exists():
        p.unlink()
        return JSONResponse({"ok": True, "deleted": job_id})
    raise HTTPException(404, "Unknown job")


@app.get("/api/job/{job_id}")
async def api_job_status(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return JSONResponse(job.snapshot())


@app.get("/api/job/{job_id}/results")
async def api_job_results(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    rows: list[dict] = []
    for r in job.results:
        if r["status"] != "OK" or not r.get("data"):
            rows.append({"sub_guid": r["sub_guid"], "sub_name": r.get("sub_name", ""),
                         "vm_family": None, "current_quota": None, "current_usage": None,
                         "pct_used": None, "status": r["status"], "error": r.get("error"),
                         "elapsed": r["elapsed"]})
            continue
        for s in r["data"].get("SkuUsages", []):
            q = s.get("CurrentQuota") or 0
            u = s.get("CurrentUsage") or 0
            pct = round((u / q) * 100, 1) if q > 0 else 0.0
            rows.append({
                "sub_guid": r["sub_guid"], "sub_name": r.get("sub_name", ""),
                "vm_family": s.get("VmFamily"),
                "current_quota": q, "current_usage": u, "pct_used": pct,
                "status": "OK", "error": None, "elapsed": r["elapsed"],
            })
    return JSONResponse({"snapshot": job.snapshot(), "rows": rows,
                          "region": job.region, "service": job.service,
                          "priority_breakdown": job.priority_breakdown})


@app.get("/api/job/{job_id}/stream")
async def api_job_stream(job_id: str) -> StreamingResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")

    async def gen():
        yield f"event: snapshot\ndata: {json.dumps(job.snapshot())}\n\n"
        if job.status in ("done", "error"):
            yield f"event: done\ndata: {json.dumps({'type': 'done', 'snapshot': job.snapshot()})}\n\n"
            return
        while True:
            try:
                ev = await asyncio.wait_for(job.queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
            if ev["type"] == "done":
                break
    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
