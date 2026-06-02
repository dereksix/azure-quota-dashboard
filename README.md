<div align="center">

# Azure Quota Dashboard

**See your Azure compute capacity the way it actually behaves — not the way the portal shows it.**

A self-hosted, single-binary dashboard that surfaces per-region quota usage,
historical trends, and the per-VM-family **On-Demand vs Spot** breakdown that
the Azure Portal doesn't expose.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](#license)
[![Cost](https://img.shields.io/badge/Azure%20cost-%240%2Fmo-success)](#cost)
[![App reg](https://img.shields.io/badge/App%20registration-not%20required-success)](#requirements)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#requirements)

</div>

---

## ✨ What you get

|  |  |
|---|---|
| 📊 **Per-region quota** | One row per VM family, with quota / used / headroom and per-sub roll-ups. |
| 🧮 **Quota groups** | Aggregates families into General Purpose / Compute / Memory / Storage / GPU / HPC / Confidential so you don't have to read raw `standardDADSv5Family` strings. |
| 📈 **Historical trends** | Every snapshot is persisted as JSON. The dashboard charts vCPU usage per day with separate OD, Spot, and Total lines. |
| 🔁 **Run-to-run compare** | Pick any two snapshots and see exactly which families and subscriptions changed — quota deltas, usage deltas, and a dedicated "what changed" panel. |
| ⚡ **OD vs Spot by family** | A live Resource Graph query enumerates running VMs and groups them by family + priority. **This is the view the Azure Portal doesn't give you.** |
| ⏱ **Auto-refresh** | One-click toggle with 5m / 15m / 30m / 1h / 4h / 24h cadences. Live countdown to next refresh. |
| 📤 **Export** | Any view downloads to CSV. Raw run JSON lives at `data/jobs/`. |

All data stays local. Nothing is sent to a third party.

---

## 🚀 Quick start

### Windows
```powershell
git clone https://github.com/dereksix/azure-quota-dashboard
cd azure-quota-dashboard
.\start.bat
```

### macOS / Linux
```bash
git clone https://github.com/dereksix/azure-quota-dashboard
cd azure-quota-dashboard
./start.sh
```

Then open <http://127.0.0.1:8765/>.

The launcher creates a virtualenv on first run, installs the four
dependencies (`fastapi`, `uvicorn`, `httpx`, `msal`), and starts the
server. Subsequent launches start in under a second.

### Sign in (one click)

| Method | When to use |
|---|---|
| **Azure CLI** | If `az login` already works on this machine — instant, zero clicks past the modal button. |
| **Device code** | If you don't have the Azure CLI installed — opens `microsoft.com/devicelogin` in any browser. |
| **Paste bearer** | Useful in CI / kiosk modes. Any token with audience `https://management.azure.com` works. |

---

## 🧠 What it actually does

```
                       ┌────────────────────────────┐
   Browser   ─────────▶│   FastAPI  (server.py)     │
                       │                            │
                       │  ┌─ MSAL token cache ──┐   │
                       │  └──────────────────────┘  │
                       │              │             │
                       └──────────────┼─────────────┘
                                      │
              ┌───────────────────────┼─────────────────────────┐
              ▼                       ▼                         ▼
     GET /subscriptions    GET /providers/Microsoft.Compute   POST /providers/
                              /locations/{r}/usages          Microsoft.ResourceGraph
                                                                    │
                          (fan-out per sub × region)         (single KQL query joined
                                                              with Compute SKU catalog
                                                              for accurate core counts)

                      ↓                                              ↓
                                  data/jobs/{snapshot_id}.json
                                  (one file per run, ~150 KB each)
```

Three free Azure APIs do all the work. There's no database, no ingestion
pipeline, no scheduler — just FastAPI fan-out, MSAL for auth, and JSON
files for history.

---

## 💰 Cost

> **Zero, for the data path.** Confirmed against current Microsoft pricing docs.

| Service | Charged? | Notes |
|---|---|---|
| ARM Compute usages API | **Free** | Standard ARM throttle: 1,200 reads/hr/sub. |
| Azure Resource Graph queries | **Free** | 15 queries / 5s / user. 1,000 rows / page. |
| List subscriptions | **Free** | One call. |
| Microsoft Entra token issuance | **Free** | n/a |

Storage is plain JSON files on whatever disk you pointed `data/` at. A year of twice-daily snapshots is ~110 MB.

The only Azure spend you might see from running this dashboard is what
**you choose** to host it on. For most users that's **$0** — they just
run it on a laptop. If you want it shared:

| Hosting choice | Approx monthly cost |
|---|---|
| Local laptop | $0 |
| App Service F1 free tier | $0 |
| Container Apps (consumption, scale-to-zero) | ~$0–2 |
| B1s VM | ~$8 |

> **Heads-up:** If someone tells you Resource Graph queries cost money,
> they're confusing it with **Log Analytics**. Both use KQL — only Log
> Analytics charges per GB ingested. Resource Graph is metered solely by
> throttle limits.

---

## 🛂 Requirements

* **Python 3.11+**
* **Reader role** on the subscriptions you want to inspect.
  Standard built-in Reader is sufficient.
* Either:
  * **Azure CLI** installed and signed in (`az login`), or
  * any browser to complete a one-time device-code sign-in.

You do **not** need:

- ❌ An app registration
- ❌ A service principal
- ❌ A managed identity
- ❌ A Cosmos DB / SQL / Storage Account
- ❌ Any new Azure resource at all

---

## 🔐 Security model

* **You bring the auth.** The dashboard mints tokens against the public
  Azure CLI clientId — pre-consented for ARM in every Entra tenant. It
  never sees your password. No tenant admin has to approve anything.
* **Tokens stay local.** Cached in an MSAL-encrypted file
  (`.msal_cache.bin`) in the repo root, ignored by git.
* **Token scope is read-only ARM** (`https://management.azure.com/.default`).
  This dashboard cannot write, deploy, or delete anything.
* **Run snapshots stay local.** Persisted JSON files contain quota
  numbers, sub IDs, and VM size names — no secrets. They're git-ignored
  by default.

---

## 📂 Layout

```
azure-quota-dashboard/
├── backend/
│   ├── server.py          # ~700 lines, single FastAPI module
│   └── requirements.txt   # 4 deps: fastapi, uvicorn, httpx, msal
├── frontend/
│   └── index.html         # single static file (Tailwind CDN + Chart.js)
├── data/jobs/             # snapshot JSON files appear here at runtime
├── start.bat              # Windows launcher
├── start.sh               # macOS / Linux launcher
└── README.md
```

---

## 🤔 FAQ

**Will leaving this running rack up Azure charges?**
No. The dashboard idles at zero cost — no polling beyond the cadence you
explicitly choose, no metered storage, no ingestion. You only pay for
the host you run it on.

**Can I use it across multiple tenants?**
Yes. Sign out and back in with a different account; tokens are per
tenant. The dashboard remembers its last cache, so two tenants is just
two clicks.

**What permissions does my account need?**
Reader on the subscriptions you want to query. The Compute usages API
and Resource Graph both honor standard ARM RBAC.

**Why does it say `lowPriorityCores` for Spot quota?**
That's the literal name Azure uses in the quota API. Spot quota is one
**regional pool** — not per-family. The dashboard surfaces both the
quota meter (regional `lowPriorityCores`) and the live spot **usage**
broken out per family (via Resource Graph) so you can see both views.

**Where does my data go?**
Nowhere. Every byte stays on your machine in `data/jobs/`.

---

## 🗺 Roadmap

- [ ] Multi-tenant view (a single user with access to multiple tenants seeing them all in one dashboard)
- [ ] Alerts / Slack webhook on threshold breach
- [ ] Reservation & Savings Plan utilization
- [ ] Cost Management overlay (free API, separate throttle)
- [ ] Export trend chart as PNG

PRs welcome.

---

## License

[MIT](LICENSE)
