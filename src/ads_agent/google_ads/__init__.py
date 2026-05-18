"""Native Google Ads API client (LWA OAuth via service account + dev token).

Authentication shape:
  - Service account `glitch-vertex-ai@capable-boulder-487806-j0.iam.gserviceaccount.com`
    (lives in project capable-boulder-487806-j0; same SA used for GA4 + Vertex)
  - Quota project `gen-lang-client-0187466920` — the GCP project the dev
    token was approved against. SA has `Service Usage Consumer` IAM on it.
  - Login customer id `<YOUR_MCC_ID>` — your MCC. Any client account
    linked under it is queryable just by switching `customer_id` per request.

Once a client (e.g. example) requests + grants account access under the MCC,
their account_id goes into `STORE_GOOGLE_ADS_ACCOUNTS_JSON` and the agent's
slug → customer_id resolution kicks in.
"""
