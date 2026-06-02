# Azure Quota Dashboard

A self-hosted dashboard that shows your Azure compute quota usage and the
**OD-vs-Spot breakdown by VM family** that the Azure Portal won't give you.

It reads directly from your own Azure subscriptions using the standard ARM
APIs and Azure Resource Graph — no app registration, no service principal,
no recurring cost.

> **Cost: $0/month for the data.** ARM Compute usages, Azure Resource
> Graph, and `/subscriptions` listings are all free of charge. The only
> thing that costs money is whatever you choose to host the dashboard on
> (and you can run it on your laptop).

---

## What you get

* **Per-region quota usage**, one row per VM family, with quota / used /
  headroom and per-sub roll-ups.
* **Quota groups** view (General Purpose / Compute / Memory / Storage /
  GPU / HPC / Confidential) so you don't have to read raw family names.
* **Historical trend** chart over every snapshot you've ever taken.
* **Compare to** dropdown to diff any two runs (delta quota, delta usage,
  changes panel highlighting +/- per family per sub).
* **OD vs Spot by VM family** — a live Resource Graph query that
  enumerates your running VMs and groups them by family + priority.
  This is what Azure Portal can't show you.

All data is persisted as plain JSON files under `data/jobs/`. Nothing
leaves your machine.

---

## Requirements

* Python **3.11+**
* Either:
  * **Azure CLI installed and signed in** (`az login`) — recommended, or
  * any browser to complete a one-time **device-code sign-in**.
* Read access to whatever subscriptions you want to inspect. The default
  permissions of a standard reader role are sufficient.

You do **not** need:

* an app registration
* a service principal
* a Cosmos DB / SQL / Storage Account
* any Azure resource other than your existing subscriptions

---

## Quick start

```powershell
# 1. clone
git clone <this repo> azure-quota-dashboard
cd azure-quota-dashboard

# 2. create a venv and install dependencies
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 3. (optional) sign into Azure CLI if you haven't already
az login

# 4. run the server
backend\.venv\Scripts\python.exe -m uvicorn server:app --app-dir backend --host 127.0.0.1 --port 8765
```

Then open <http://127.0.0.1:8765/>.

On first load:

1. Click **Sign in** in the top right.
2. Choose **Azure CLI** (instant, if you ran `az login`) or
   **Device code** (opens a browser, takes ~10 seconds).
3. Pick a region (e.g. `eastus`, `centralus`), click **Run**.
4. Wait a few seconds. The job will refresh quota data, then run a single
   Resource Graph query for the OD-vs-Spot breakdown.

---

## Architecture

```
                      ┌─────────────────────────┐
   Browser  ─────────▶│  FastAPI (server.py)    │
                      │                         │
                      │  ┌─ token cache ─────┐  │   (MSAL + az CLI)
                      │  └───────────────────┘  │
                      │            │            │
                      └────────────┼────────────┘
                                   │
                  ┌────────────────┼─────────────────┐
                  ▼                ▼                 ▼
         GET /subscriptions   GET .../usages   POST /providers/
                              (per sub × region) Microsoft.ResourceGraph
                              ARM Compute API      (KQL: VMs by SKU+priority)

         All free. No per-call billing. Throttle limits are very generous.

                                   │
                                   ▼
                          data/jobs/{id}.json
                          (one file per snapshot)
```

* **`/api/subs`** — lists every enabled subscription the signed-in user
  can read (live, paginated through ARM).
* **`/api/job` POST** — kicks off a snapshot run. Concurrently calls
  Compute usages for every selected sub, then a single Resource Graph
  query joined against the Compute SKU catalog for the OD/Spot
  breakdown.
* **`/api/jobs/trend`** — aggregates every persisted run into a
  per-day time series.
* Frontend is a single static HTML file (Tailwind CDN + Chart.js).

---

## Hosting options

| Where | Cost | Notes |
|---|---|---|
| Local laptop | $0 | Best for individuals / one-off audits. |
| Azure App Service **F1** free tier | $0 | Easy multi-user share inside a corp net. |
| Azure Container Apps (consumption, scale-to-zero) | ~$0–2/mo | Best modern-cloud option. |
| B1s VM | ~$8/mo | If you want it always-on. |

Storage is just JSON files — point `data/` at OneDrive / SharePoint /
Azure Files for shared snapshots.

---

## What costs money?

**Nothing in the data path.** Specifically:

| Service | Charged? |
|---|---|
| ARM Compute usages API | Free |
| Azure Resource Graph queries | Free |
| List subscriptions | Free |
| Microsoft Entra token issuance | Free |

The only Azure spend you'll see in your bill from running this dashboard
is whatever compute / storage you choose to host it on (which, if you
follow the recommended path of "run on a laptop" or "App Service F1
tier", is also $0).

If someone tells you Resource Graph queries cost money — they may be
confusing it with Log Analytics. Both use the same KQL syntax but only
Log Analytics charges per GB ingested. Resource Graph is metered solely
by throttle limits (15 queries / 5s / user, 1000 rows / page,
20 MB / response).

---

## Frequently asked questions

**Q: Will this rack up charges if I leave it running?**
No. The dashboard idles at zero cost — no polling, no schedules, no
ingestion meters. You only pay for the host you run it on.

**Q: Can I run it against multiple tenants?**
Yes — sign out and back in with the other tenant's account. Tokens are
single-tenant. (Multi-tenant support is straightforward to add but not
included by default.)

**Q: What permissions do I need?**
Reader on the subscriptions you want to query. The Compute usages API
and Resource Graph both honor standard ARM RBAC.

**Q: Can I export?**
Yes — the **Export CSV** button dumps the current run's row table.
The raw JSON for any run is at `data/jobs/{job_id}.json`.

---

## License

MIT.
