# Changelog

## [3.8.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.7.2...v3.8.0) (2026-02-08)


### New Features

* add admin device management endpoints ([c57de10](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c57de1081a9e905ba191f64c37221c36713c82a6))
* add admin traffic packages and device limit management ([2f90f91](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f90f9134df58b8c0a329c20060efcf07d5d92f9))
* add admin updates endpoint for bot and cabinet releases ([11b8ab1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11b8ab1959e83fafe405be0b76dfa3dd1580a68b))
* add endpoint for updating user referral commission percent ([da6f746](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da6f746b093be8cdbf4e2889c50b35087fbc90de))
* add enrichment data to CSV export ([f2dbab6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f2dbab617155cdc41573d885f0e55222e5b9825b))
* add server-side sorting for enrichment columns ([15c7cc2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/15c7cc2a58e1f1935d10712a981466629db251d1))
* add system info endpoint for admin dashboard ([02c30f8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/02c30f8e7eb6ba90ed8983cfd82199a22b473bbf))
* add traffic usage enrichment endpoint with devices, spending, dates, last node ([5cf3f2f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5cf3f2f76eb2cd93282f845ea0850f6707bfcc09))
* admin panel enhancements & bug fixes ([e6ebf81](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e6ebf81752499df8eb0a710072785e3d603dba33))


### Bug Fixes

* add debug logging for bulk device response structure ([46da31d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/46da31d89c55c225dec9136d225f2db967cf8961))
* add email field to traffic table for OAuth/email users ([94fcf20](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/94fcf20d17c54efd67fa7bd47eff1afdd1507e08))
* add email/UUID fallback for OAuth user panel sync ([165965d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/165965d8ea60a002c061fd75f88b759f2da66d7d))
* add enrichment device mapping debug logs ([5be82f2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5be82f2d78aed9b54d74e86f261baa5655e5dcd9))
* include additional devices in tariff renewal price and display ([17e9259](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/17e9259eb1d41dbf1d313b6a7d500f6458359393))
* paginate bulk device endpoint to fetch all HWID devices ([4648a82](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4648a82da959410603c92055bcde7f96131e0c29))
* read bot version from pyproject.toml when VERSION env is not set ([9828ff0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9828ff0845ec1d199a6fa63fe490ad3570cf9c8f))
* revert device pagination, add raw user data field discovery ([8f7fa76](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8f7fa76e6ab34a3ad2f61f4e1f06026fd3fbf4e3))
* use bulk device endpoint instead of per-user calls ([5f219c3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f219c33e6d49b0e3e4405a57f8344a4237f1002))
* use correct pagination params (start/size) for bulk HWID devices ([17af51c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/17af51ce0bdfa45197384988d56960a1918ab709))
* use per-user panel endpoints for reliable device counts and last node data ([9d39901](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d39901f78ece55c740a5df2603601e5d0b1caca))

## [3.7.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.7.1...v3.7.2) (2026-02-08)


### Bug Fixes

* handle FK violation in create_yookassa_payment when user is deleted ([55d281b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/55d281b0e37a6e8977ceff792cccb8669560945b))
* remove dots from Remnawave username sanitization ([d6fa86b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d6fa86b870eccbf22327cd205539dd2084f0014e))

## [3.7.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.7.0...v3.7.1) (2026-02-08)


### Bug Fixes

* release-please config — remove blocked workflow files ([d88ca98](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d88ca980ec67e303e37f0094a2912471929b4cef))
* remove workflow files and pyproject.toml from release-please extra-files ([5070bb3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5070bb34e8a09b2641783f5e818bb624469ad610))
* resolve HWID reset and webhook FK violation ([5f3e426](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f3e426750c2adcb097b92f1a9e7725b1c5c5eba))
* resolve HWID reset context manager bug and webhook FK violation ([a9eee19](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a9eee19c95efdc38ecf5fa28f7402a2bbba7dd07))
* resolve merge conflict in release-please config ([0ef4f55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ef4f55304751571754f2027105af3e507f75dfd))
* resolve multiple production errors and performance issues ([071c23d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/071c23dd5297c20527442cb5d348d498ebf20af4))

## [3.7.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.6.0...v3.7.0) (2026-02-07)


### Features

* add admin traffic usage API ([aa1cd38](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aa1cd3829c5c3671e220d49dd7ec2d83563e2cf9))
* add admin traffic usage API with per-node statistics ([6c2c25d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6c2c25d2ccb27446c822e4ed94d9351bfeaf4549))
* add node/status filters and custom date range to traffic page ([ad260d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ad260d9fe0b232c9d65176502476212902909660))
* add node/status filters, custom date range, connected devices to traffic page ([9ea533a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ea533a864e345647754f316bd27971fba1420af))
* add node/status filters, date range, devices to traffic page ([ad6522f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ad6522f547e68ef5965e70d395ca381b0a032093))
* add risk columns to traffic CSV export ([7c1a142](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7c1a1426537e43d14eff0a1c3faeca484611b58b))
* add tariff filter, fix traffic data aggregation ([fa01819](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fa01819674b2d2abb0d05b470559b09eb43abef8))
* node/status filters + custom date range for traffic page ([a161e2f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a161e2f904732b459fef98a67abfaae1214ecfd4))
* tariff filter + fix traffic data aggregation ([1021c2c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1021c2cdcd07cf2194e59af7b59491108339e61f))
* traffic filters, date range & risk columns in CSV export ([4c40b5b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4c40b5b370616a9ab40cbf0cccdbc0ac4a3f8278))


### Bug Fixes

* close unclosed HTML tags in version notification ([0b61c7f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0b61c7fe482e7bbfbb3421307a96d54addfd91ee))
* close unclosed HTML tags when truncating version notification ([b674550](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b6745508da861af9b2ff05d89b4ac9a3933da510))
* correct response parsing for non-legacy node-users endpoint ([a076dfb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a076dfb5503a349450b5aa8aac3c6f40070b715d))
* correct response parsing for non-legacy node-users endpoint ([91ac90c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/91ac90c2aecfb990679b3d0c835314dde448886a))
* handle mixed types in traffic sort ([eeed2d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eeed2d6369b07860505c59bcff391e7b17e0ffb7))
* handle mixed types in traffic sort for string fields ([a194be0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a194be0843856b3376167d9ba8a8ef737280998c))
* resolve 429 rate limiting on traffic page ([b12544d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b12544d3ea8f4bbd2d8c941f83ee3ac412157adb))
* resolve 429 rate limiting on traffic page ([924d6bc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/924d6bc09c815c1d188ea1d0e7974f7e803c1d3f))
* use legacy per-node endpoint for traffic aggregation ([cc1c8ba](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cc1c8bacb42a9089021b7ae0fecd1f2717953efb))
* use legacy per-node endpoint with correct response format ([b707b79](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b707b7995b90c6465910a35e9a4403e1408c6568))
* use PaymentService for cabinet YooKassa payments ([61bb8fc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/61bb8fcafd94509568f134ccdba7769b66cc7d5d))
* use PaymentService for cabinet YooKassa payments to save local DB record ([ff5bba3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ff5bba3fc5d1e1b08d008b64215e487a9eb70960))

## [3.6.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.5.0...v3.6.0) (2026-02-07)


### Features

* add OAuth 2.0 authorization (Google, Yandex, Discord, VK) ([97be4af](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97be4afbffd809fe2786a6d248fc4d3f770cb8cf))
* add panel info, node usage endpoints and campaign to user detail ([287a43b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/287a43ba6527ff3464a527821d746a68e5371bbe))
* add panel info, node usage endpoints and campaign to user detail ([0703212](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/070321230bcb868e4bc7a39c287ed3431a4aef4a))
* add TRIAL_DISABLED_FOR setting to disable trial by user type ([c4794db](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c4794db1dd78f7c48b5da896bdb2f000e493e079))
* add user_id filter to admin tickets endpoint ([8886d0d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8886d0dea20aa5a31c6b6f0c3391b3c012b4b34d))
* add user_id filter to admin tickets endpoint ([d3819c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d3819c492f88794e4466c2da986fd3a928d7f3df))
* block registration with disposable email addresses ([9ca24ef](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ca24efe434278925c0c1f8d2f2d644a67985c89))
* block registration with disposable email addresses ([116c845](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/116c8453bb371b5eacf5c9d07f497eb449a355cc))
* disable trial by user type (email/telegram/all) ([4e7438b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4e7438b9f9c01e30c48fcf2bbe191e9b11598185))
* migrate OAuth state storage from in-memory to Redis ([e9b98b8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e9b98b837a8552360ef4c41f6cd7a5779aa8b0a7))
* OAuth 2.0 authorization (Google, Yandex, Discord, VK) ([3cbb9ef](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3cbb9ef024695352959ef9a82bf8b81f0ba1d940))
* return 30-day daily breakdown for node usage ([7102c50](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7102c50f52d583add863331e96f3a9de189f581a))
* return 30-day daily breakdown for node usage ([e4c65ca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e4c65ca220994cf08ed3510f51d9e2808bb2d154))


### Bug Fixes

* increase OAuth HTTP timeout to 30s ([333a3c5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/333a3c590120a64f6b2963efab1edd861274840c))
* parse bandwidth stats series format for node usage ([557dbf3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/557dbf3ebe777d2137e0e28303dc2a803b15c1c6))
* parse bandwidth stats series format for node usage ([462f7a9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/462f7a99b9d5c0b7436dbc3d6ab5db6c6cfa3118))
* pass tariff object instead of tariff_id to set_tariff_promo_groups ([1ffb8a5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1ffb8a5b85455396006e1fcddd48f4c9a2ca2700))
* query per-node legacy endpoint for user traffic breakdown ([b94e3ed](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b94e3edf80e747077992c03882119c7559ad1c31))
* query per-node legacy endpoint for user traffic breakdown ([51ca3e4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/51ca3e42b75c1870c76a1b25f667629855cfe886))
* reduce node usage to 2 API calls to avoid 429 rate limit ([c68c4e5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c68c4e59846abba9c7c78ae91ec18e2e0e329e3c))
* reduce node usage to 2 API calls to avoid 429 rate limit ([f00a051](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f00a051bb323e5ba94a3c38939870986726ed58e))
* use accessible nodes API and fix date format for node usage ([943e9a8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/943e9a86aaa449cd3154b0919cfdc52d2a35b509))
* use accessible nodes API and fix date format for node usage ([c4da591](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c4da59173155e2eeb69eca21416f816fcbd1fa9c))

## [3.5.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.4.0...v3.5.0) (2026-02-06)


### Features

* add tariff reorder API endpoint ([4c2e11e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4c2e11e64bed41592f5a12061dcca74ce43e0806))
* pass platform-level fields from RemnaWave config to frontend ([095bc00](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/095bc00b33d7082558a8b7252906db2850dce9da))
* serve original RemnaWave config from app-config endpoint ([43762ce](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/43762ce8f4fa7142a1ca62a92b97a027dab2564d))
* tariff reorder API endpoint ([085a617](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/085a61721a8175b3f4fd744614c446d73346f2b7))


### Bug Fixes

* enforce blacklist via middleware ([561708b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/561708b7772ec5b84d6ee049aeba26dc70675583))
* enforce blacklist via middleware instead of per-handler checks ([966a599](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/966a599c2c778dce9eea3c61adf6067fb33119f6))
* exclude signature field from Telegram initData HMAC validation ([5b64046](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5b6404613772610c595e55bde1249cdf6ec3269d))
* improve button URL resolution and pass uiConfig to frontend ([0ed98c3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ed98c39b6c95911a38a26a32d0ffbcf9cfd7c80))
* restore unquote for user data parsing in telegram auth ([c2cabbe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c2cabbee097a41a95d16c34d43ab7e70d076c4dc))


### Reverts

* remove signature pop from HMAC validation ([4234769](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4234769e92104a6c4f8f1d522e1fca25bc7b20d0))
