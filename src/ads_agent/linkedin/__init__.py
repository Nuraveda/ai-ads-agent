"""Native LinkedIn Marketing API client.

Reuses the same OAuth app + tokens as glitch-social-media-agent
(see `LINKEDIN_CLIENT_ID` in .env). The founder's access token
already carries the Advertising API scopes (`r_ads`, `rw_ads`,
`r_ads_reporting`) — verified live against `/rest/adAccountUsers`.

Multi-tenant pattern (no MCC equivalent on LinkedIn):
clients add the founder's LinkedIn user as CAMPAIGN_MANAGER on
their ad account in Campaign Manager → Manage Access. After that,
the account is queryable just by switching `account` URN per request,
same as the Google Ads MCC pattern.
"""
