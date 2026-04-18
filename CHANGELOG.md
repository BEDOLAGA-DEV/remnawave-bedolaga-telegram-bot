# Changelog

## [3.49.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.48.0...v3.49.0) (2026-04-18)


### New Features

* integrate AuraPay payment provider ([9717936](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97179360c0288940b1fa2f6c21a6e1431a27536f))


### Bug Fixes

* add missing RollyPay CRUD wrappers and guest payment flow ([0f814be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0f814be1b7dfaec84dde9acc402b2c1790417611))
* align campaign top registrations revenue with period comparison ([16bc1d4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/16bc1d41989d66e105724ed846fb40ccf03322fd))
* handle edge case when all tariffs are daily in legacy renewal ([29877fc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/29877fc93bc612ee199ccb2438c90e57a3c1e9e0))
* rate-limit daily subscription insufficient balance notifications to 6 hours ([ecc4a61](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ecc4a6147dad0c8886acd48f38e567a6c7fc8916))
* redirect legacy users without tariff to tariff selection on renewal ([5986c00](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5986c00fab8c5fe2060d296afca72d28038ff7bd))
* register PayPear and RollyPay in admin panel settings ([2aa5927](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2aa59274331610eec3cd84f90ddb49ee59da22ef))


### Documentation

* add AuraPay to README with partner block ([25ea5c6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25ea5c60fdaf3cac9294b6df74142c176b1d4d04))
* add PayPear and RollyPay to README with partner blocks ([1c696c6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1c696c69e34e668ff90c915249b7c3dc37bfd89b))
* add PayPear and RollyPay to README with partner blocks ([b531959](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b53195998231d34b6e1c7165193e87fee6e5c293))

## [3.48.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.47.0...v3.48.0) (2026-04-16)


### New Features

* integrate PayPear payment provider ([a18f6ca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a18f6caa9bd9c08511c464e6141fb8aa614135b0))
* integrate RollyPay payment provider (SBP via USDT) ([ccc2f4e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ccc2f4efecf0a3c1a943971c81a9a7d1985ae14a))


### Bug Fixes

* increase nalogo receipt queue retry window to 12 hours ([92eaf45](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/92eaf4531162e5625b938bafc43eb98abe2632e2))
* low balance alerts disabled by default, add quiet hours, expiry filter, top-up button ([2d5afe5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2d5afe5d75ff65f4d167a853e078943d518a881f))
* show menu buttons for limited (traffic exhausted) subscriptions ([0c54549](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c545490b61baec930f10adc86652a7e5cf5d378))
* show menu buttons for limited subscriptions in back-to-menu paths ([61cf495](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/61cf495fc5919e699aba7231f49222722da044b4))
* support payment_method selection for RollyPay (sbp/card/crypto) ([a598582](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a59858227f31bd53d0ac54d693288ad52e447687))


### Documentation

* add SEVERPAY, PAYPEAR, ROLLYPAY to .env.example ([25447ed](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25447edc9eab7c3a76ed94be52c1b341917a40c2))

## [3.47.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.46.1...v3.47.0) (2026-04-15)


### New Features

* multi-tariff sync fix, daily discount fix, campaign links, TELEGRAM_API_URL ([4db9e85](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4db9e850629f25cbcb11bb5ba0e0de5c580ca115))

## [3.46.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.46.0...v3.46.1) (2026-04-13)


### Bug Fixes

* add checkfirst guards to cabinet_refresh_tokens migration ([8587f03](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8587f03f67d7a451b07a4d9c450bc4524f4ac0e7))
* add missing migration for cabinet_refresh_tokens table ([4707cdf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4707cdf60c9d163c1191719b3b1fc4a17ae993d2))
* cabinet_refresh_tokens migration + notification_settings jsonb ([0274738](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/02747381dce2b96af7f97b5f41d6acff5d9d8fd3))
* change notification_settings from json to jsonb for DISTINCT compatibility ([e74fda9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e74fda954ccc63e2ac7a30933d9f9ac26772b25d))

## [3.46.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.45.2...v3.46.0) (2026-04-13)


### New Features

* add broadcast category (system/news/promo) + filter recipients by user prefs ([931abfe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/931abfe7a5a7fb70e9638fbf6b566fa8d1a837e4))
* add category field to broadcast API schemas and routes ([0300044](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0300044b009f3e4b3aa3928652dfaf261a387dbc))
* add RemnaWave retry queue for failed API calls (BUG-2, BUG-10) ([abdf296](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/abdf2967675975e90f0c4d834f129281c1c28e7b))
* add remnawave_resync_service for identity-change sync ([b57f185](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b57f185258be050d945cec5989ad6dc710980a6a))
* add traffic % warning check using user's threshold preference ([1d96f80](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1d96f80f60ca445eb5108e7bc54e00d022a4cc9e))
* add user notification preferences helper utility ([e0e2edf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e0e2edf81659fbeea361046d1bb2718c2149d884))
* implement low balance alert + respect user notification preferences ([4e50419](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4e50419171176ee452371ff095bdfead3879e554))
* respect user subscription_expiry notification preferences ([63fdfe4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/63fdfe4a421942b26caca35d8bd9b1d65f1fe7e2))
* respect user traffic_warning notification preference in webhook handler ([7208a52](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7208a52c9424d39757187fc86eec2c3460a2cdbb))
* save campaign_slug during standalone email registration ([a8e2b62](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a8e2b62f4bb0833ca32446b34ad4e0c5615fcd2a))
* start RemnaWave retry queue on app startup ([8f1882f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8f1882f24c7d066e2d0fc756f38ce23bf082687e))


### Bug Fixes

* add retry queue to all remaining RemnaWave error handlers ([7e920fa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7e920fa30fc8e61ed2dd31e7a09151d57b7361ca))
* add retry queue to cabinet subscription operation RemnaWave errors ([1b376ba](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b376baeca120970b1ffcd406b1bbf7d9d43cee0))
* add retry queue to classic mode bot purchase handler ([970dc54](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/970dc549dfa06ba9945d5bd861374058acdca86b))
* add retry queue to daily subscription service RemnaWave errors ([65120f0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/65120f0badc9a4581ba0a127acdae8a0b23e8501))
* add retry queue to payment webhook and renewal service RemnaWave errors ([91a756a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/91a756a33ed4ce685bdf485cdb4e91c3e08799dd))
* add TRAFFIC_WARNING_ALERT and LOW_BALANCE_ALERT localization keys to all locales ([2321667](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2321667ecbe76bfe8dd37213e0bb4e45104e0fc5))
* always sync squads in auto-purchase renewal (BUG-4) ([8542a39](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8542a393055a93d320d5c8c6d3aa7cc291cf8def))
* default sync_squads=True in update_remnawave_user (BUG-4) ([6aed7d3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6aed7d355bc47c4dbd2aa761d78a5e5421c32edf))
* enforce max_attempts limit in NaloGO receipt queue ([16d9163](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/16d91638bc149c5eee8f4cdd266cbd195c411030))
* enqueue retry on RemnaWave API failure in all purchase flows (BUG-2, BUG-10) ([9cb559f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9cb559ff3994c0f1c6ba48a4ec09dec9b391e48b))
* exclude users with active subscriptions from expired broadcast ([1eeeb39](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1eeeb39779982ebb2700cb7523760631229407da))
* handle TelegramBadRequest when deleting old ticket notifications ([eb18b3a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eb18b3a0f9ac3a617a1b21d3293e1619980a2a71))
* match tariff_id when creating subscriptions from panel sync (BUG-11) ([646ac4c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/646ac4cfa18f738040fbc6498c5d86c1546e2b9a))
* protect OAuth users with remnawave_uuid from sync deactivation (BUG-6) ([cf19e4e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cf19e4e1f7148b21d9a8072f7a0b4ae97fd04e8a))
* raise MAX_BUTTONS_PER_ROW to 8 and allow tg:// deep links in menu editor ([570af82](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/570af82dfdec980f42be96c8f816e1677f896f81))
* resync RemnaWave after account merge (BUG-7) ([9c08ce6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9c08ce69485b78f8fe818500e05ab6995115166a))
* resync RemnaWave after Telegram account linking (BUG-1) ([d465ccb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d465ccb3ac3a86add6313d6bb61d53d0fe143d5e))
* sync connected_squads from panel during sync (BUG-5) ([35412e9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/35412e9f215680c0fdf1c55b5bf23496f662935c))
* trial activation fallback to trial-eligible servers when tariff has no squads (BUG-12) + fix misleading button text ([be32010](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/be32010d63966498bc6216823a75d328346d9a37))
* upsert refresh tokens (ON CONFLICT) + periodic cleanup of expired/revoked tokens ([fb8d2b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fb8d2b3ee4566823840100b96fe2f3bc7d41edb7))
* use 'is not None' for telegram_id in create_user API (BUG-9) ([8623521](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/862352139e8a3545e144b0618fed5011470a9a67))
* use MAX_DEVICES_LIMIT instead of hardcoded 10 for device buttons ([bc3893b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc3893b934f0d4e5059eedbd03cdfbc628852ac1))
* use update_remnawave_user when UUID exists in tariff_purchase (BUG-3) ([a1b6d9b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a1b6d9bb619ec3de038647fe6f2e5d38979298a8))

## [3.45.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.45.1...v3.45.2) (2026-04-08)


### Bug Fixes

* batch bug fixes from user complaints ([31adcfd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/31adcfded4b161bf515d4d6b25b4395e543208f4))
* batch bug fixes from user complaints ([78f963b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/78f963bf5e7b3439d7614584c4041f88be5beb4a))
* исправление парсинга черного списка (поддержка '#' и извлечение username) ([357d94d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/357d94d1b0d7fc8036b00cbb6b75175c29821751))
* исправление парсинга черного списка (поддержка '#' и извлечение username) ([2f71846](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f7184627a0fb598a8c0208905cdecf0e4bb04a7))

## [3.45.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.45.0...v3.45.1) (2026-04-03)


### Bug Fixes

* add missing WEBHOOK_TORRENT_DETECTED mapping + dedup before uniq… ([4165eae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4165eaea7adfdaf683b1ece16c8c93a9c4ed216d))
* add missing WEBHOOK_TORRENT_DETECTED mapping + dedup before unique index in migration 0053 ([3b5d5a1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3b5d5a18a1122ef50868fd038a09109d17795a74))

## [3.45.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.44.0...v3.45.0) (2026-04-03)


### New Features

* send torrent blocker notification to user (not just admin) ([2f9d003](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f9d00343bee2980cc89bd24361259073b97127a))


### Bug Fixes

* resolve multiple subscription bugs — LIMITED status, trial tariff blocking, traffic reset strategy, classic mode pricing, 100% discount support ([9b7ac47](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9b7ac47f16076e546da62062ff7ce18d7c308988))
* restore missing import + rewrite user.deleted webhook to properly deactivate all subscriptions ([819f09a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/819f09a68ec95237294bae97f31c644044a3623f))
* subscription system bugfixes + torrent notifications + user deletion cleanup ([7d24e8d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7d24e8d7047c7a3a1c417e655a6fbccbe5ae577d))

## [3.44.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.43.1...v3.44.0) (2026-04-02)


### New Features

* add SberPay as KassaAI sub-method (payment_system_id=43) ([9d63635](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d636355026ad1e50d045e78ffa21e76cfef0774))


### Bug Fixes

* address review issues in PR [#2829](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues/2829) webhook intentional deletion guard ([977950b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/977950b97f07eecf089152d3f4e678fda373e1e6))
* autopay failure notifications ignoring 6h cooldown ([991f0b4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/991f0b43e1e73446690a4fbec7c5c5642ac8c406))
* middleware disables panel VPN for all subs ignoring per-channel settings ([f284351](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f284351c51a6843db0771a92338ec770d5f0d8d2))
* NameError in SeverPay guest payment flow ([2d42152](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2d42152f5491b14cc45388e0ffccf8a61848a2f6))
* notification sent for non-deactivated subs + webhook race condition ([b04157c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b04157c91327d9031e9f603a6ad33c708e27d753))
* Pal24 card/sbp option not passed to API in cabinet balance topup ([6713921](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/67139218878dca3e75974eb5b5a2ce91d5b1438e))
* prevent nested state saves and None state loss in promo handler ([b607993](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b607993854d1374e7d7c2afbb7fe5cc8824732f5))
* promo code activation destroys balance input FSM state ([2466590](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/246659032de812f1d4502029ab139f3104237d5c))
* remove non-existent Platega method code 10, rename 11 to Карты (RUB) ([033d0da](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/033d0da5e0033a2310431586a291b529e3ccb89a))
* send telegram_id@telegram.org as email to Kassa AI ([3dc72b0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3dc72b00e751a69966d2d5830492c82e055b72e6))
* send telegram_id@telegram.org as email to SeverPay ([08ca947](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/08ca947b2b2bb29782c86e7b5d6bea71e2811751))

## [3.43.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.43.0...v3.43.1) (2026-03-31)


### Bug Fixes

* prevent MissingGreenlet on subscription.tariff lazy load in webhook handlers ([72170b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/72170b35f5d2af56aa7dcb579a70ecf6af2da3f6))
* use subscription-level remnawave_uuid in multi-tariff mode for sync and detail pages ([0c284b9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c284b9e9941b516170fc68a3f63551096cb5a7b))


### Documentation

* add Platega partnership to README, highlight partner payment providers ([312cc72](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/312cc728a9321fe9ac90cf1f5201e38465f67f16))

## [3.43.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.42.0...v3.43.0) (2026-03-29)


### New Features

* add Remnawave panel 2.7.0 API support ([565c083](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/565c08366bdc1d3dcaba89a8522360e1e4d8c2d8))
* add SeverPay support to cabinet balance top-up ([092b9f6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/092b9f63b24e8129a8e6f9ac0040f19a35514295))
* add subscription_id to admin sync endpoints for multi-tariff ([54a19a9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/54a19a9c50d58affd1cd8bc897e360318744e28d))
* add tariff identification to all notifications for multi-tariff mode ([7dd67e3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7dd67e36b3a8a408239ec53d0ae2cc230dfd726c))
* add tariff_id to promo codes for trial subscription type ([63e4296](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/63e4296197d7943914267f503782e46befc151d4))
* **api:** expose email field in UserResponse ([23d1830](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/23d1830644be6ab71da629465604cbb296fa7c02))
* DELETE /subscriptions/:id for expired/disabled subscriptions ([c27f144](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c27f144b76ad392c5d6909f8f62cb2b065515eed))
* expose MULTI_TARIFF_ENABLED and MAX_ACTIVE_SUBSCRIPTIONS in admin settings ([2628012](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2628012097893ec93cfcb2b8fe190183cfe53a3b))
* expose per-inbound traffic breakdown in nodes realtime API ([5d173c8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5d173c806af1e3c31854ae92b194016984cfd80e))
* include countryEmoji and providerName in realtime metrics ([b59c581](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b59c581e916e4f6511514463e149a03cc4fb6f8f))
* multi-subscription support ([1099c52](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1099c5224c07b4ddc17397b0200f64f22fbf8520))
* multi-subscription support (1 user = N subscriptions) ([335be66](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/335be66980bfefae9d39e8f549fb4df1780ce3d0))
* return is_daily and is_daily_paused in subscription list API ([4dd8170](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4dd81702ceb5ee9ff46af16249d0dd145da2e3b6))
* support email/OAuth users in referral editing and add remove endpoints ([7f60196](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7f60196033fa48f093edd06da8f2162936c76162))
* trial lifecycle + purchase-options filter for multi-tariff ([048d208](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/048d208bc1aa188c16fc4e01de836e9f1553b561))
* wheel subscription picker for multi-tariff mode ([24edfb6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/24edfb6c3f83726aff0a7b4566da5762ceb10d72))


### Bug Fixes

* accept subscription_id from query param in renew endpoint (consistent with other endpoints) ([824d54b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/824d54b7dc97a7a4df8a7f34e078e3ff018df442))
* account linking broken in multi-tariff mode (MULTI_TARIFF_ENABLED=true) ([2c12a47](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2c12a4773c8b0614a06acaf5bcef2784cc211a5a))
* account merge no longer nulls transferred subscriptions' remnawave_uuid ([b0273dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b0273dc8aeecbbdd3a72c41c885492026f0aea58))
* add missing ADMIN_PAYMENTS localization keys for ru and en ([4f76f53](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4f76f53d55588387251b80de6ac878589299817e))
* add period_days validation and zero-price guard to tariff purchase ([cefdfc5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cefdfc54cc8baea3b4e36740d6913b74a8072b6a))
* add redirect_slashes=False to prevent HTTP 307 redirects on subscription endpoints ([f7f8ea8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f7f8ea87cfd5b182e7e395a9aa95c37bd40e165c))
* add selectinload for GuestPurchase.user/tariff in gift activation ([84357a1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/84357a1e8715cb176191eace7d019544175822f4))
* add tariff identification to remaining notification gaps ([cddb8d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cddb8d63326e2849d674de64b2da508103d5922f))
* add_traffic handler passes FSM state to resolve_subscription for multi-tariff context ([684f286](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/684f286fcd9d2c29b1c02d5fc7c9ebf5631f4878))
* address remaining review issues in device limit patch ([9726145](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/972614511fb10d2cad91e0b8a52cac4c634fc6d7))
* address review issues in device limit patch ([34aec03](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/34aec0323bd78ff159c9e84bdcf584051b3b0fd4))
* admin handlers use _resolve_admin_subscription + per-subscription UUID ([147ef6b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/147ef6b22b9518fc806939aba435f81121828641))
* admin panel per-subscription UUID in multi-tariff mode ([56fffc2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/56fffc241572ec1d59370bfdb01945877e34e5a0))
* admin server/devices/traffic buttons pass subscription_id in multi-tariff ([9a27e6d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9a27e6db3110e171a9cac02728a21cd4a002e9b1))
* admin tariff purchase now creates separate RemnaWave user per tariff ([95ba739](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/95ba73995820ac5c154226a370979437809d3f8c))
* assign promo group from tariff on guest purchase ([da11ec6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da11ec6f946e6a439111c31c6e5387dab4167303))
* async tariff loading in promocode serialization ([3bec662](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3bec6620b67f0dfe7926a030ee98ef0f08e5673e))
* auto-purchase processes each autopay subscription independently ([f89e326](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f89e326a19f37ec2189fe01814cd3a4cf69c4d02))
* back button in subscriptions list uses correct callback ([382e29d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/382e29d3dd20cca8f231e444181703bf72c45c86))
* back buttons in devices/traffic return to subscription detail in multi-tariff ([319941d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/319941d33a43552c09ba5fb95e1a165dd1603999))
* block classic subscription renewal/autopay when tariff mode enabled ([cd6913c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cd6913cb849ccefe2b36de786068890c8117b1d8))
* block legacy subscription renewal bypass in tariff mode ([78209c8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/78209c862320ef030134ac7e77c57229b5701cfd))
* cabinet admin create subscription now creates new RemnaWave user in multi-tariff mode ([31c67d1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/31c67d15657d88df24ba490e0d47a9c3104883af))
* cabinet purchase_tariff — handle IntegrityError with compensating refund ([1bc2581](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1bc2581669ecc3b47b12283c7fb6988bbe34cb28))
* cabinet routes use smart subscription fallback + per-subscription UUID ([f83ff26](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f83ff26332c8057f5cbb1725f1d5db74e23af9bf))
* centralize trial cleanup in CRUD + shared subscription resolver for bot ([355fef8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/355fef846e786b638fdcf8823fc077ace89e8615))
* comprehensive multi-subscription audit fixes across routes, handlers, and services ([d071269](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d071269b8c4df08f91b3a94e09d3a37686082f8a))
* comprehensive tariff switch/extend/back button fixes for multi-tariff ([e42bddb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e42bddb868414aec8df55c071adab63a88b30696))
* contest prize applies to best non-daily subscription in multi-tariff ([d2bbeb8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d2bbeb8624ca1320b38a300de0417f64747a6286))
* daily tariff switch uses _resolve_subscription instead of searching by new tariff_id ([4e12ab3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4e12ab3458c1c73ed4ffe62ca8688ff917a64e4c))
* delete subscription from RemnaWave panel + prevent phantom webhook notifications ([a12ffb1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a12ffb1d6c30bc2d73888f5c352c12fc3d0fd95c))
* device limit decrease, HWID pagination, tariff max enforcement ([931eeb3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/931eeb35689532a892e14bc76a05fea1a8f2cec3))
* devices button shows menu with buy + manage options in multi-tariff ([f925efb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f925efbfb47de8dc8e5932eab629ee6beb7c61e2))
* disable redirect_slashes globally to prevent HTTP 307 on subscription endpoints ([06feb3f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/06feb3fff589616b6cf78a2cdf6ba5c33cca7794))
* distinguish cabinet gift notifications from landing page ([48eaa6b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/48eaa6b0724a3f881d144abc723fa8373ee9a67f))
* eligibility and display use best non-daily subscription in multi-tariff ([76ba19d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/76ba19da175553597599fcbcc8513221078e01f6))
* fix MiniApp renewal options 500 error for legacy subscriptions ([aa36549](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aa36549bb3bc1e04362750b7eecee23f9ba99d88))
* gift code activation and multi-tariff subscription sync ([f93c51a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f93c51a6773e706a6fc6f9b6016868e181400762))
* harden node info display against injection and type errors ([6d167d2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d167d292266a660f0d6fd906db3cdea84e8c9cf))
* harden webhook signature verification across all payment providers ([8295880](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/82958801b5179ff5703e6d11b3b0b30f74581eeb))
* import Subscription in wheel_service to fix NameError ([34bb87c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/34bb87c7baf34b4d9c0c09a6b3fe2b76378d9a66))
* improve UX for legacy users migrating to tariff mode ([f6f330d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f6f330db4a2e6aa5d14a616b9fd2365577f928a1))
* load buyer relationship before gift notification, clean up recipient logic ([adb39c6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/adb39c6ef46507966482cdceed8ba03e861285ee))
* multi-subscription support for promocodes, contests, phantom merge ([6d468e9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d468e9adacc8c111cdb5145dc048f7eea6e93e4))
* multi-subscription UUID resolution and ownership validation ([d87fb47](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d87fb47e886eebb3a1e75b1dae1982230f7bac72))
* multi-tariff code review — 13 critical/high bugs fixed across 14 files ([5724906](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57249065178900a8626181556119464c7d55fd74))
* multi-tariff MEDIUM/LOW batch — 20 issues across 17 files ([94ed282](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/94ed282381fcc71efdef8b55c2e566b2fcd92c2c))
* multi-tariff Stage 2 critical fixes — panel sync, guest purchase, cart isolation ([4259ba1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4259ba1cb57020314f478d7efc78c915fce9e9b6))
* multi-tariff Stage 2 HIGH fixes — 18 issues across 12 files ([c6bedc6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c6bedc6a061cd6ff2010f1345d6bae42e4282fbd))
* multi-tariff Stage 3 critical fixes — panel sync UUID, admin grant, wheel ([49db5f5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/49db5f5eedb452aac7b12739ce735abb8c3b99d2))
* multi-tariff Stage 3 HIGH fixes — phantom, cart, yookassa, auto-extend ([aa7e461](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aa7e461c4451fc477ac9a1714c8e2243fe12ba49))
* multi-tariff Stage 4 critical fixes — keyboards, guest purchase, monitoring, tariff deletion ([a49e52c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a49e52cc92819b10d1d4afc823cf98b534a84a0f))
* multi-tariff Stage 5 fixes — auth sync, notifications, cart, race guard ([948e479](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/948e4791f49a0626100af8ada29da8b605392566))
* multi-tariff sync auto-links legacy user-level UUIDs to subscriptions ([dbe247b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dbe247ba6f8cea2ccb845ceb1cfd87d76d47dc52))
* notifications include tariff name for multi-subscription clarity ([05d1ae0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/05d1ae0560f63627c17b629d476088ce21398863))
* parse_bytes now handles IEC units (GiB, MiB, KiB) from API ([1471320](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/147132060659e828b005160003d5bff1a565e280))
* pass FSM state to _resolve_subscription across all subscription handlers ([90fb0a2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/90fb0a21e227d46ed5624051d8185a1e026b0806))
* pass sub_id to show_devices_page to fix NameError in multi-tariff ([59d4b35](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/59d4b353a6456fc40682239df441b23bc8d63f6d))
* persist referral to Redis on /start to prevent loss when user opens miniapp ([6d9bd99](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d9bd9915cda01d819b4c3db02115777c8cb7500))
* post-payment keyboard checks all subscriptions instead of LIMIT 1 ([25b853d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25b853d629f9581a9f7871142751dcb2b1e73d3a))
* prevent sync from overwriting wrong subscription traffic in multi-tariff mode ([78a7eaf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/78a7eafcb65a175dd8f5ded304138ebd8c47acd8))
* prevent sync/from-panel cross-subscription data mismatch ([960aa44](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/960aa44b007b454f94403cedf98033c9a2c60dec))
* promocode system broken in multi-tariff mode ([3cbe09d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3cbe09ddd54bec6568b49fbd2ed961649160d21f))
* re-fetch subscription after lock_user_for_pricing to prevent selectinload reset ([71082f4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/71082f436c6a49409b84e84a367e6059abea850e))
* remnawave service uses per-subscription UUID throughout multi-tariff ([afd7b6d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/afd7b6d7ec777fd380b43c77da55d3114ff837d7))
* RemnaWave sync finds user by Subscription.remnawave_uuid in multi-tariff ([c3c2b81](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c3c2b8137b5b8e656b0368742e55a99c4d6a3ec3))
* remove unused imports and variables after rebase ([b6cf361](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b6cf361737653f7601515126b2e2d073a8a5ca0f))
* remove user.subscription setter - use local variable instead ([2f88b07](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f88b07f05cb568c6a6f0bde9c7b7e91d4309d66))
* remove UUID fallback override in admin_tariffs + restore promo on IntegrityError ([a232d21](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a232d21edd9714082a7943f9e23db4241f188d66))
* rename refresh('subscription') to refresh('subscriptions') in all files ([e99f3d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e99f3d9a374b1ab32bbefd8be2036aa185e50f5c))
* renewal handlers use _resolve_subscription + store subscription_id in FSM ([87bf65c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/87bf65c8097d270a139402ab5e976b6215735c99))
* renewal status check, int() safety, daily charge atomicity ([58d899a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/58d899aab89ff41ef86a2c578613a59720fdab31))
* renumber multi-subscription migrations to avoid conflicts with dev ([5a7b3d5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5a7b3d59627f61b3afc7d23ec08d00d9c07c7f10))
* resolve MissingGreenlet error on article detail view ([004dac5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/004dac5b7ef76b2b1e98e0ab0f62d9fe7fcbd12b))
* services use smart subscription selection + per-subscription UUID ([0866c2e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0866c2ea4b3183dc63e5ea88391d83dd03f30428))
* set is_daily_paused=True when admin cancels/disables daily subscription to prevent auto-resume ([d04f2fc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d04f2fc718d11a4766627fda83bdee3abf7f15ea))
* show all subscriptions in main menu for multi-tariff mode ([e39c358](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e39c358d5caf00674c8d148757b66d46b15485c2))
* show subscription picker for traffic/connect buttons with multiple subs ([a39e355](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a39e3554d8b91eac676cf4e3b4cfe6a0dd9caa82))
* suppress empty reward alerts and clean up referral notifications ([e3d8d21](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e3d8d21b662761dca1ffe5938e3ce4d72275be84))
* tariff purchase shows purchased tariffs and blocks re-buying in multi-tariff ([9644135](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9644135dd7a1c1e951274a73a56ec75fd715fe62))
* tariff_purchase next() fallbacks use None instead of active_subs[0] in multi-tariff ([72d5bae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/72d5bae531ae04a36fbc44a6803de7f17020f846))
* test access promo applies to all active subscriptions in multi-tariff ([181ef15](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/181ef1501b31837585958f89631c1aa36b1872a2))
* transliterate Cyrillic slugs instead of stripping to 'untitled' ([c805cfd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c805cfd6d8335f147991458e42fa8a19121a5126))
* trial promo extends existing subscription with same tariff ([b8662b8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b8662b8bf6c341f391a25940dea928a043396f04))
* trial reset in multi-tariff only deletes trial subscriptions, keeps paid ([424fff4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/424fff4ac29e697268eeb57d5b5bacd498c691e8))
* trial subscription lifecycle — autopay, cleanup on purchase, bonus days ([344852b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/344852b85223b49fc52f1401d63b198e1a8412c5))
* use '/' instead of empty path in subscription sub-routers ([07ebc43](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/07ebc435cf2544c31e0c55bb543775ffcd252e01))
* use empty path instead of '/' for multi-tariff list endpoint to avoid 404 ([c8ecec4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c8ecec47a0af59911407d8d5a9d6df3f2afc3fb8))
* UUID check in servers/tariff_switch, start.py refresh, delegation state passing ([bd46b4c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bd46b4cf6dcd7cd65a42456d741b957a22aa7863))
* UUID warnings, phantom merge, yookassa validation, contest prize notification ([fe03b58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe03b587db96eb365a718e21a0177dd6e6e6a480))
* validate_and_clean_subscription uses per-subscription UUID in multi-tariff mode, not user-level UUID ([18f31c5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/18f31c565c7c0217ebd2348389466c16dc17fc3f))
* web API routes use multi-subscription resolution for operations ([a1623d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a1623d94b119025f59263749eaa727bc910dc467))


### Refactoring

* remove dead multi-tariff check in guest purchase activation ([34b5a9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/34b5a9ab3a0eab8561ddd6930b59b817bc2ef9b4))
* update remnawave API integration for v2.7.0 ([173cc37](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/173cc374bb8391359d8f54569565f18a82fc28eb))


### Documentation

* add Stage 3+4 audit results to multi-tariff review ([6dc5879](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6dc5879ffa7edb77166291fbda9e03fbe532bc2d))
* update multi-tariff review with Stage 2 full audit results ([40d2ec6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/40d2ec67189064b41774292029a397fde2a1c863))

## [3.42.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.41.0...v3.42.0) (2026-03-23)


### New Features

* add managed news categories and tags with DB-backed CRUD ([51392d1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/51392d1918d8e2e94645acfb3a11b8e16776a5d5))
* add media upload/delete API for news articles ([a0d40ad](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a0d40ad432d858ebfe75485a9597d32e847d5746))
* add news articles module with admin CRUD and public API ([b932403](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b93240393f739f1243bbbfdd4298b90974b8fa87))
* enforce single featured news article — unfeature others on toggle/create/update ([b5853ec](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b5853ec3b6769655f8d19914e51fd078ec9edccc))
* show Platega payment methods inline on main screen ([#2720](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues/2720)) ([334db53](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/334db53868ae2f9206fdde97fa575e953a83cbcf))


### Bug Fixes

* add explicit File(...) to UploadFile param to fix 422 on media upload ([89bfdc8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/89bfdc8ed6bfba48cfcb61083240d5e6e870f49b))
* add Literal type to SavedMedia and close orphaned PIL Image objects ([ce554cb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ce554cb2a8e606b6060dbf63463e64087a3a7539))
* add user ID to payment descriptions for all providers and fix tuple bug ([2f19c76](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f19c76357fe7de1636d65968cb13f784ee47c31))
* catch DecompressionBombError, hoist MP4 brands to module level ([172924d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/172924df0e1248b27d3f18f6acf23cf115040941))
* comprehensive html.escape() for all user/admin data in Telegram HTML messages ([9de3490](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9de34900a2a9047ab275c0bbf16fd314eb49a3ec))
* comprehensive security hardening across payment and API layers ([8175bc8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8175bc8bfe56dc564a0783c45226451b695bdbd6))
* correctly price unlimited traffic (0 GB) in classic subscription mode ([aec04f0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aec04f0085bd9c566bd033b8bb628389ff22bdf6))
* create uploads subdirectories in Dockerfile for correct permissions ([5ed3780](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ed3780f830023f7b1940dc96256fd38e3c86f4b))
* media upload security hardening from 6-agent review ([165d25e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/165d25ef5fc02eecf7c6d703072b6adbe88bfd98))
* news module security hardening, perf optimizations, bug fixes ([2b91808](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2b91808b0c72381cfb2f4e36eaff69e9102a32ee))
* phantom user merge on claim failure, referral assignment, account merge hardening ([fad77f8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fad77f8c80a8fdecb0512f0ff91e5ae6d78ec8f3))
* register categories/tags/media routers before news to avoid route conflict ([d9cda3a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9cda3a6d67c6249397e3778c1aefc6bdb6d9e4f))
* reject HEIC as MP4, close UploadFile, narrow exception handling ([7ff73e8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7ff73e8492343be5ed77fad5eea7e730621eac76))
* remove future annotations breaking UploadFile, harden media URL generation ([0225fa1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0225fa155b7d4f3d1c6572a68651c206b1820db0))
* replace asyncio.gather with sequential queries on shared session ([3e69efe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3e69efe5891bbe8869a7feb2320185e14d83dd73))
* respect per-channel disable_on_leave settings in monitoring service ([958ec48](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/958ec489a2a1d01d19cb6e60f52fb2be56295104))
* respect X-Forwarded-Proto in media URL generation to prevent mixed content ([fd41009](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fd410096ea7e541d8443ec4336bfd72de63d03d9))
* restore connected_squads and admin notification on daily subscription resume ([89341ba](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/89341baa6243496d3b24c501e227ed156676c052))
* simplify 0046 migration downgrade to just drop_table ([015c2da](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/015c2da297e4250ebfa50e32b3c1468c9a2143f3))
* suppress harmless TelegramBadRequest errors and fix discount promo display ([0fe3c21](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0fe3c217f752ced2e3c3f56b4ab5b4c898ba2d8e))
* use IF EXISTS in downgrade for FK indexes ([76b1f9b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/76b1f9b036e276646546aa4c89a3fe1d2ee58a40))
* validate FK existence, add FK indexes, expand video brand whitelist ([f0cdd5d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f0cdd5dc904926b55be390798a71a5427a20949b))
* validate period_days against tariff in purchase-tariff and auto-purchase ([4660ca5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4660ca5756f5f1ef8033f9202d68612d570ddb1a))


### Refactoring

* extract phantom service, replace lightweight merge with execute_merge ([6658af6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6658af6268c10db170e3eaabc75f857e5c664c3c))
* simplify referral invite text to single template ([cbe630c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cbe630cab0973d7d71dd72603b114807b237ac84))

## [3.41.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.40.0...v3.41.0) (2026-03-22)


### New Features

* add subscription status to referral network graph nodes ([de91d32](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/de91d3282ffa15c0cec60c0d62871d39e7ee4c05))
* add total subscription revenue to referral network stats ([2bdb764](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2bdb7643f8fd142e99caee0fe989348161377348))


### Bug Fixes

* add abs() to all remaining subscription payment sum queries ([1eb4e18](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1eb4e18c1776b2265a48e0b923a0ca4ee057d912))
* add missing total_subscription_revenue_kopeks in scoped graph early return ([bcc761f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bcc761f9d3f673bd2b404adf817762058d8e0df4))
* consider subscription status field in network graph ([454dc93](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/454dc9321bb9405c5ff0ff559ae4ced15533f3af))
* superadmin role managed exclusively via env config ([e0bedc8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e0bedc8e780a2f91509517110639773e90bb6125))
* treat expired and limited subscription statuses as inactive in referral network graph ([5ed2f0c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ed2f0c95842a43ab57220dc05ca346748bd6adb))
* use abs() for subscription payment amounts in referral network ([056c13b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/056c13bc23e6737f44bbcb802a66b643349f75a9))


### Refactoring

* extract _compute_subscription_status shared helper ([8b8f1b9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8b8f1b91f37f829528f785a40e3a9cb98c85e043))

## [3.40.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.39.0...v3.40.0) (2026-03-22)


### New Features

* allow inactive tariffs for trial subscription activation ([cce3b0c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cce3b0c13bcbf0b567bd4dcf2670973382e7cab0))
* custom broadcast buttons and fix home button to use bot menu ([13ea376](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/13ea3768b516337c4e0320120bc60a9acb27a16b))


### Bug Fixes

* accept stale Telegram initData to prevent MiniApp auth failures ([4c2cb63](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4c2cb63cf9f71fb392c3723a99e88ca3d02b127d))
* daily subscription pause not persisting in cabinet and miniapp ([d3c9940](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d3c994083e3b054d02d4911172968c914724d051))
* handle spurious user.deleted webhooks — preserve active subscriptions and prevent orphaned panel users ([9eab802](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9eab80200006e576967204b52f90bf9866875917))
* prevent MESSAGE_TOO_LONG in promo groups list ([c307278](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c30727823169159b4b6b61f54897b209ced8dfd2))
* referral system — self-referral protection, race condition fix, deleted user re-registration ([ed5a92a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ed5a92ab966dac54c15217050eae87f4b05eed62))
* sanitize email dots in RemnaWave username generation ([6c20858](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6c208581d936f5ab7d6b978baafd50881b8ce9f1))
* send DISABLED instead of EXPIRED status to RemnaWave API ([79cfcbc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79cfcbcece3938f2daa83206f96ec1bffd0857e0))

## [3.39.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.38.0...v3.39.0) (2026-03-21)


### New Features

* add NaloGO fiscal receipts for code-only gift purchases ([90209eb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/90209ebef1a872665e622124a1898d52eff398e7))


### Bug Fixes

* add NaloGO fiscal receipt creation for landing page purchases ([4244962](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/424496233773b4cee4e389a1172e95208b3afeaf))
* manual admin top-ups missing from sales statistics ([ab43e74](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ab43e74ab7484f8d3517f91e366ea395e1944b99))
* skip non-JSON payload rows in cryptobot payment index and query ([ba79d03](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba79d03e389afed972296fe2bc05104aa6b883f3))

## [3.38.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.37.0...v3.38.0) (2026-03-21)


### New Features

* add SOCKS proxy support for nalogo (tax service) module ([3c5bf4f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3c5bf4fa22d1cdf144269f4e6ab32a4523c8f1f3))


### Bug Fixes

* add diagnostic payload logging in create_user error path ([4990ddf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4990ddf9e46495b65fc3638ea8d6bed0cbe6b857))
* retry Remnawave API calls without externalSquadUuid on A039 FK violation ([de00612](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/de006129657ce3dac2b1f2fc0ab1b91e23e44241))
* sanitize proxy credentials in all nalogo error paths ([3bf3105](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3bf31055e71ff64e6a6d94486bb7f7775ac7dc91))

## [3.37.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.36.1...v3.37.0) (2026-03-21)


### New Features

* add SOCKS5 proxy support for Telegram API traffic ([82b6a8b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/82b6a8bf707736541b58637b0fc9a84b0c403a6c))
* broadcast caption validation + landing daily created stats ([d400cd7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d400cd7b49cb18edf8545a5af54009561610218a))
* expose cabinet_email/password vars in subscription delivered template admin UI ([f82a713](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f82a713110c90494e41f2de9910f5dc60a06962e))
* include cabinet credentials in subscription delivered email ([fedcf25](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fedcf2569a153ac40699dd5b402208ac86db0fc3))
* show both bot and cabinet referral links everywhere ([67da390](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/67da3903715e97c0d84d0018efdd74b085ee8720))


### Bug Fixes

* default payment_method to BALANCE for bot subscription payments ([226d3f2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/226d3f2766bb0842e5a4aefb1458a9e40389108e))
* derive income_today from revenue_chart to ensure consistency ([1882909](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1882909b3e60f4959921ae085e909bc91c0756b3))
* include landing page revenue in dashboard statistics ([c6c1599](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c6c1599e14a8c7033dfa399ff3f2c3ee6d156598))
* include SUBSCRIPTION_PAYMENT in dashboard revenue ([5f71eaa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f71eaa92604faa2c6928994bd32a9c6c8ae7ae5))
* include SUBSCRIPTION_PAYMENT in dashboard revenue calculations ([13dba5a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/13dba5a303604f8d39e85d1382a4d36225f957ac))
* include SUBSCRIPTION_PAYMENT in dashboard revenue calculations ([d7f91c8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7f91c83584384ef5d0fc90a28debc722b2db79f))
* include SUBSCRIPTION_PAYMENT in recent payments today/week totals ([6982d27](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6982d27378d0dbe5c7b37d1491e6c9d0461dffc8))
* include SUBSCRIPTION_PAYMENT in sales summary and deposits stats ([27ef752](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/27ef75214e73211a68d541f5375ca14275ea86e9))
* increase landing purchase rate limit from 5 to 30 req/min ([801921f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/801921ff74107daccc17e49a0335c7b133268280))
* narrow exception handling and fix session leak in gift.py ([3875335](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3875335cd7083115043d22f8726f0d33a225cd10))
* prevent bootstrap from reactivating revoked superadmin roles ([9dd6b54](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9dd6b54c6e963d2a842d4ac48c9a463014306ed7))
* prevent double balance credit on concurrent Platega webhooks ([0e1296e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0e1296e0ea291ee661aa82508d5eb8049ce23cac))
* prevent double-payment TOCTOU race in all payment providers ([82c79c1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/82c79c130601736cd149350f761dcceacfb7c2db))
* resolve EmailService stale SMTP config causing NoneType crash on from_email ([2a72dea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2a72deadd6ac3baea15a8d4ee414d763c16690b0))
* resolve remaining TOCTOU issues in RioPay, SeverPay and restore paid_at ([afefcc9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/afefcc9c07a80ecf65871bc2d0fbcfe13ffc24fd))


### Refactoring

* centralize Bot instantiation via create_bot() factory ([0a53b85](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0a53b85b8a3193f67e148c0f2bde246cc92f010c))

## [3.36.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.36.0...v3.36.1) (2026-03-20)


### Bug Fixes

* handle duplicate admin roles in RBAC bootstrap ([877b1cd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/877b1cde11c462c0cf5692119ca09944476a2fb6))
* make migration 0042 idempotent for retry_count column ([5faf701](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5faf7015ac043f44366fd2a5e00be19eab76b945))

## [3.36.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.35.0...v3.36.0) (2026-03-20)


### New Features

* add media attachment support for admin ticket replies ([69bb399](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69bb399b63d6e1d761cc5518e6272917fc5a6ae7))
* add multi-provider recovery, retry_count, amount verification, and indexes ([3d78974](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3d78974af70b360449d9cf634e09a79821cdc7c0))
* add partner → campaign edges to referral network graph ([01132a7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/01132a7bc77b07eaaaf876c05d639bfed83e5324))
* add referral network graph visualization admin API ([c08c903](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c08c903e8f94f3730872b19de904ce166ba35b98))
* add scoped referral network graph with scope selector API ([df086b0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/df086b09c75a9157cdc558fb610b617a2d49deaf))
* multi-select scope for referral network graph API ([6a4ce3d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6a4ce3dd38dc3cf2e9db08322093bfd0b84f1e1c))


### Bug Fixes

* address review findings for guest purchase recovery ([57c5c67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57c5c679eef987e9bacee1a9d420ac3c0ff69ff7))
* address review findings for multi-provider recovery ([79c110f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79c110ff41659ff225164c13690bafc859212d05))
* allow repeated auto-assignment of promo groups on each purchase ([4a002b7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4a002b7db1bfa8fd1149b0e3dfa469e8912a0e3b))
* correct revenue calculations in referral network ([c8f4cca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c8f4cca34053713eb2793bbb82e74cbb9a6f893c))
* improve referral network query correctness and cleanup ([ac9fcd8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ac9fcd8d30dd64fdc7363e689e9ffaf03700976e))
* prevent duplicate promo groups during auto-assignment after purchase ([da7a9cc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da7a9cc3c5fd771b932a2bfd154400f6f923a4d1))
* prevent guest purchases from getting stuck in PENDING/FAILED status ([2781236](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2781236011942e794949544b9c5422aa8679e5eb))
* prevent premature commits in promocode promo group operations ([8b26680](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8b2668087b4831c08474846b20591b2158d607fc))
* propagate exceptions from get_primary_user_promo_group ([3ec9e71](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3ec9e71de7d8d40e969f9ea738455445d04d983a))
* use 'kassa_ai' base model name for guest metadata patch ([182667e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/182667ecb86f9bbbe87d640865dcbcaadf9e72f6))
* use base model name for KassaAI guest metadata patch ([b6d4373](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b6d43739337cd2e2cfd61ac33398eb6def5b28b6))


### Performance

* add covering indexes for referral network queries ([b5471b7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b5471b7720213c217fc452dc1234a7d3c53447d5))

## [3.35.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.34.1...v3.35.0) (2026-03-18)


### New Features

* add user_email to admin payments API response ([7101555](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7101555da0722d1eacd97f40b6b8c8c3a2327a0c))
* include manual admin top-ups in sales statistics revenue ([b80eeea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b80eeea089568c60c20b1ae165b8dbe887bbe378))
* раздельные топики для админских уведомлений ([e15b18f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e15b18fb41b180e7dd3d65f2f058667be321fe85))


### Bug Fixes

* fix Platega and CryptoBot webhook verification ([b40a812](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b40a812f3aa0596bf6c5105008451dd8a17b103f))
* handle None autopay_days_before in autopayment processing ([f1d4534](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f1d45343e941594d69f71e822ecc9b3a7062f4bf))
* remove forced white background from custom email template overrides ([cb61014](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cb61014d9c5a89e3aeafb191f1b9826ca1cbf338))
* undefined currency variable in RioPay payment creation ([5b33534](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5b3353433bc524e3e51f2ae87d26bd162bd9f97b))

## [3.34.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.34.0...v3.34.1) (2026-03-18)


### Bug Fixes

* add null check for subscription in execute_change_devices ([5081deb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5081debee7625954bd7b82f66e09dd58e08e8822))
* correct CryptoBot webhook signature verification and auto-fill topup amount from cart ([d7ad9d7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7ad9d70330b6ef5599f7a9409cdd16657d61b85))
* correct RioPay API header case and remove undocumented fields ([1a87d43](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1a87d438fe127a0b62bd6ee59887212869a3cb17))
* disable quick amount buttons in balance topup ([d0eab3f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d0eab3f7aacf0249ca244f168c96044347c918e3))
* reset device limit to new tariff base on tariff switch ([aec01ce](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aec01ce0d4a36da5ddd07b56ca4bd5de04735a0b))
* sync crypto link from happ.cryptoLink in webhook handlers ([0ceff44](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ceff44c30cf11466c4cbe51b520558c51c4af4a))


### Refactoring

* remove quick amount buttons feature entirely ([136f29c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/136f29c1eb63778b2b329ed5bf72ab06a4531d0b))


### Documentation

* add bot preview screenshot to README ([d7931a2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7931a2afaf272aae74453f2bb6d493593895f67))
* add cabinet preview screenshot to README ([b032c8f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b032c8f35435ddd592df04d6352097580f5e9837))
* add icons and list all 14+1 payment providers ([1306c24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1306c24fa36fd3e812c5bec551a0aca450ca7d2c))
* add Redis to tech stack ([c14d7ab](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c14d7ab0af2e5dd4f35213b323a97eedcc99af0e))
* add Redis to tech stack ([e12cc9f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e12cc9f248764104d538db1daaede9ddd8b77b2d))
* add WATA partnership block to payments section ([31bdf8a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/31bdf8a0aeba53fcec40358d0a38b8480130a346))
* increase logo size to 800px ([22e7f15](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/22e7f150b30c35b0419e7923e94c5ebc92c4b61a))
* redesign README — concise feature showcase, link to docs ([38deb70](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/38deb70f8118d371e36ac92a4417bdb543635fc9))
* replace header logo with new artwork ([6888822](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/688882237fe3019bb78d5e1d7ad54faf4cd69c09))
* WATA partnership block with logo and table card ([8d5a002](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8d5a0029964ba52b40e03ebfe8ab6f4146d3aca9))

## [3.34.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.33.0...v3.34.0) (2026-03-18)


### New Features

* добавлен SeverPay в админ-панель и настройки кабинета ([06a00e3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/06a00e367c303b5426008f38a20393ec4f2e07cd))
* добавлена интеграция SeverPay для пополнения баланса ([abaf279](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/abaf279533d31994a8a70346627c962b43000c64))
* поиск платежей в админ-панели с фильтрами и статистикой ([1804c28](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1804c28f0551fbe52883fc36fc4cfcd60d2d6bd6))


### Bug Fixes

* remove contains_eager conflicting with selectinload on user relationship ([fddf8ef](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fddf8ef5ebc8e5176a75a10e92d59165bdabf2e1))
* добавлен импорт MAX_ALL_TIME_DAYS в admin_payments routes ([ad26832](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ad268329be45bdd665ce4a6f142e9762a78e55b5))
* добавлены RioPay и SeverPay в REAL_PAYMENT_METHODS ([f967c29](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f967c29bd7cfe8a66e7e0f492573bb7521fcda22))

## [3.33.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.32.4...v3.33.0) (2026-03-17)


### New Features

* add SBP and Card sub-options for KassaAI payment method ([5b722c5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5b722c521036befcbbaf6192215f651c2ec9c4fb))
* add SBP and Card sub-options to kassa_ai payment method ([04419fd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/04419fdff7dc244eb2c9553ea1501e2de454010b))
* deep link авторизация в кабинете при блокировке oauth.telegram.org ([322d457](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/322d45765220c854e36ae0b4b862a96d36ae3be8))
* добавлена поддержка RioPay в кабинете ([3d1fbc7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3d1fbc70f8add81d4a3561d63dcd46f385bcc6f1))
* добавлена поддержка RioPay для лендингов и подарков ([04f4e6b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/04f4e6bf6e9031ef8512a07ab36355111c524319))


### Bug Fixes

* add back button to payment amount validation errors ([20eff61](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/20eff6170fb752051022a14fa9d927d59ff1d602))
* add sync_squads=True to admin tariff change handler ([3f0b24c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3f0b24c1ec82ebfb6fdbc801ed6b493ea09c9a2f))
* deep link auth security and reliability fixes ([099391e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/099391eb5f24319703f12ae2cb72d93b26164d99))
* enforce promo group authorization on country/server selection ([641da94](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/641da949a907ade7b870e1a5385fdb71f9e524af))
* merge phantom users into active accounts on /start ([77f1a76](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/77f1a764d59d68236899ec59c884907d3666d3dd))
* MissingGreenlet crash after subscription purchase in cabinet ([a80a85c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a80a85c2a489f397efb4ffb5bd77655420a49adb))
* MissingGreenlet crash after subscription purchase in cabinet ([1cc687a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1cc687ac15ecdf12928e5ce448514c09b6628f65))
* MissingGreenlet при изменении количества устройств на CLASSIC подписках ([826accb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/826accba519f23a687fcc3d387f727fd31d2a88c))
* protect external squads from deletion during server sync ([b563796](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b563796091e83edcc8dfbd6222648b5346f39a9c))
* review findings — db.commit, isinstance guard, constants, ACTIVE check ([72b5305](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/72b5305b870ae9ecdb2672549b9c153dd8b3f7bc))
* sub-method enabled check, guest payment provider, silent FSM return ([603b9a1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/603b9a1f4610a5b288d6fba78c98474c439529aa))
* **subscription:** remove stale extend promo state fields causing NameError ([20a6fa1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/20a6fa1bcf362455623f43838f4ffaaf98b33e76))
* swap Caddy auth headers — api_key to Authorization, caddy_token to X-Api-Key ([038c34e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/038c34e52a863d0c5c6993ea785587ba7e0bc61d))
* sync squads to Remnawave panel on tariff purchase/switch ([c34fdd1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c34fdd10a0a22a85a4e29cbf44da2ac5d4a643b3))
* защита внешних сквадов от удаления при синхронизации серверов ([f84885c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f84885cc8aa0c9a70f916076e987284f1f9c3479))
* исправлен расчёт конверсии в статистике продаж ([3089c17](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3089c1704b54323b4c2a151d40a5b395a427c658))
* исправлены проблемы RioPay интеграции после ревью ([4abb8cb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4abb8cb1a3f21089697199261bcc37e6f6a5c623))
* миграция Tribute webhook с deprecated user_id на trb_user_id ([9419941](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/94199413c283167edd829b337cf9bec0a5414a54))
* скрыть плашку верификации email при выключенной верификации ([4966e39](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4966e39eb9b92967ef92c92ae434ca6cdea80c84))


### Refactoring

* deduplicate KassaAI handlers with config dict and shared helpers ([e4bb043](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e4bb0430fb9cc4f55013767ceda6d1c214bd80a6))
* move KASSA_AI_SUB_METHODS to service layer, add early enabled checks ([cda2392](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cda239241122ae1dd02a252b3eadc47453c0c48b))

## [3.32.4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.32.3...v3.32.4) (2026-03-16)


### Bug Fixes

* лог полного payload при ошибке PATCH /api/users для диагностики A039 ([8d7f0ee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8d7f0eea0fecd9e66bf199bd2c288e073f1354c0))
* не пересылать activeInternalSquads в рутинных обновлениях RemnaWave (A039) ([4aaf0dd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4aaf0ddd25527ec23fa6a479ac3826d6b6266761))
* не пересылать externalSquadUuid в рутинных обновлениях RemnaWave ([3d68db0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3d68db0a51fac55640d44be784c832875ca2da17))
* расширен лог PATCH /api/users payload для диагностики A039 ([db2f0c9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/db2f0c93f2974410e744411fb9111c6de1f0f0be))
* режим «Контакт и тикеты» возвращает support_type='both' вместо 'tickets' ([2f33e55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f33e5514469f2686c4b35e2105f4188a41d4145))
* реферальный бонус инвайтера — сумма вместо максимума, защита флага первого пополнения ([e1bcb1b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e1bcb1ba910ef3a79dec5fa974ae8e6c09494aa7))
* сохранение user_id до rollback чтобы избежать MissingGreenlet при lazy load ([3f8e899](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3f8e8993b2949b5a5e04b1d8a468ef7dc1170e08))
* убрана отправка externalSquadUuid=null в RemnaWave API и исправлен ложный лог синхронизации рулетки ([f80912e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f80912e444ab5706c809e689ca5ed2a38da118d0))
* уведомление об истечении подписки теперь учитывает autopay_enabled пользователя ([c0b282a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c0b282a189a2b761c68fc70886edd91d9c807ff6))
* устранена отправка externalSquadUuid=null в RemnaWave API (A039) и исправлен reduce_devices ([e453521](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e4535210982351413cb82483000fe441e7b7300a))


### Refactoring

* централизация всех расчётов цен в PricingEngine ([8d3cd50](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8d3cd500980f4f640cb1ba493150f1f20e8bd58c))

## [3.32.3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.32.2...v3.32.3) (2026-03-14)


### Bug Fixes

* campaign registration, revenue calculation, backup restore, autopay errors, referral links ([7648707](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7648707ca26d6cd2703b50b0fe8c4697e6155784))
* implement case-insensitive email checks in authentication and user retrieval ([7e466ef](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7e466ef464ce918d885bd6297d1e605a633fd43e))
* implement case-insensitive email checks in authentication and user retrieval ([ebee834](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ebee8348ca338b9be5f044537e5e2b4740dc6441))
* **payment:** prioritize saved cart after topup over expired auto-extend ([28321df](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/28321df4d274269536efebcf3da870f2e7d07d90))
* refresh CLASSIC_PERIOD_PRICES when admin changes PRICE_*_DAYS or SALES_MODE ([6adf70b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6adf70b2da6e2250cc8e909dbb497b355302e72f))

## [3.32.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.32.1...v3.32.2) (2026-03-13)


### Bug Fixes

* add nested selectinload and referrer eager loading to prevent MissingGreenlet ([3306e02](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3306e029021c396e13774a205225beece4fbbcfb))
* add selectinload to user lock queries to prevent MissingGreenlet ([5442f28](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5442f288d4c6c3973dd92ac141172a9f0e53a28f))
* silence PARTICIPANT_ID_INVALID error in channel subscription check ([14dceaa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/14dceaa39ff9faa1c9205483653014a1c5ac73fb))

## [3.32.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.32.0...v3.32.1) (2026-03-13)


### Bug Fixes

* invalid ISO date format in node usage stats API call ([69a38da](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69a38dad259bd05f4658e1014ce0bd73fc2e2ac5))
* platega webhook ID fallback for SBP and card payments ([aa3459b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aa3459b8463ce0a54b7709aa3547b2337064fa26))
* resolve MissingGreenlet in switch_tariff endpoint ([4d695be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4d695be7d51adda40fa72c00c349fb0e1ec4acd2))

## [3.32.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.31.0...v3.32.0) (2026-03-13)


### New Features

* add _calculate_servers_price (fixed fallback) and _calculate_traffic_price ([88369ee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/88369eec5047e733d26d2450a74abd0d600b2e1b))
* add CLASSIC_PERIOD_PRICES to config ([c3bb63f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c3bb63ffed6e0b684c322aa51d70ab7e71c8eb6b))
* add LIMITED subscription status and preserve extra devices on tariff switch ([8f43452](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8f434525eb14618e3c3e26261d443b1632c111bb))
* add RenewalPricing dataclass and PricingEngine discount methods ([83ca51c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/83ca51cd5b040e747c6db904dde0f3a5c59f480f))
* implement calculate_renewal_price with tariff and classic modes ([02e5401](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/02e5401327786c9dfe5ae7d4c89624c9455aa53e))


### Bug Fixes

* add missing settings import in admin_users tariff switch ([b2ee6c7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b2ee6c766a1fb0c9a701684a6349b970d12f5e2e))
* add per-category discounts and months multiplier to classic mode ([1660b24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1660b24f9844374bbd156f9202a8e1550a6beb49))
* add period_days whitelist validation and type annotations ([18e2e78](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/18e2e7841a6d614263e7c87db5964916ec869a9d))
* address 6-agent review findings for PricingEngine ([c9f2dff](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c9f2dffabf6369df360c5f9ad7a12c0415026310))
* address review findings from 5-agent audit ([08bea70](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/08bea704ded78102dce29deac8da95c4e4b9d815))
* atomicity refactor, review fixes, and DELETED recovery logging ([ba54819](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba54819f9cd7f60914dd472b68885683f435db4e))
* change None assignment to [] + add "or []" guards at all 5 call sites. ([a5fbd74](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a5fbd7400f828824c5baa520bdaf06023b4caf70))
* downgrade known-harmless RemnaWave 400s to warning level ([0419781](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/04197817fede058dc4688dce2f9877f0fc2a7f7f))
* guard rollback on commit flag, add flush to promo_offer_log ([b7775b7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b7775b72dc7a1b3d18f179c2f247fe9f47023347))
* handle legacy telegram_id in YooKassa webhook recovery metadata ([815a1d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/815a1d9136f39b932d4b369aec9d67034d6785d9))
* harden remnawave API error handling and YooKassa user cross-validation ([585baaf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/585baaf63c9f535e5311a32085d0187d8c854001))
* harden YooKassa webhook recovery user lookup ([d35ee58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d35ee58aa6f3edc6a9e8ab43025569262acf64a2))
* payment providers — lock_user_for_update + commit=False atomicity ([b4ef52c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b4ef52caa4b324eded8e6c6cb715a09ad59140c1))
* prevent balance loss on auto-purchase for DISABLED subscriptions and fix WATA expiration ([266340a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/266340aad195995f208ed82fc11e0909d34898f4))
* pricing audit — display/charge parity, race conditions, balance locks ([ae99358](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ae99358ae9f35a25370ab127d98a0b630a08e3f2))
* resolve merge conflict with dev (accept calc_device_limit_on_tariff_switch) ([ba049ca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba049ca017e004c25f8738f01b2d5f329a35bb5e))
* user deletion FK error + connected_squads None TypeError ([a5fbd74](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a5fbd7400f828824c5baa520bdaf06023b4caf70))


### Refactoring

* add typed breakdowns + module-level singleton to PricingEngine ([b551def](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b551def3402e2bf762406fb0b374360958231bb3))
* extract shared formatting helpers into app/utils/formatting.py ([5e9a462](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5e9a462261e46ee649de481266a821fd6793bf2e))
* make finalize() accept both old and new pricing types ([3efa24b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3efa24bab3a2bd1d31103b134e502c10af8e41e1))
* migrate admin user price calculation to PricingEngine ([49c0f3f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/49c0f3fc10d27092961601cf7f6a780fb56885fa))
* migrate all callers to pricing_engine singleton + fix miniapp discount ([e24b911](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e24b911283bf4cbee7b18d3e49c935217e4a2863))
* migrate bot renewal display to PricingEngine ([ce82c2c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ce82c2c00988542ac73dc9d2e811711ea9cefebe))
* migrate bot renewal execute to PricingEngine ([acf27a1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/acf27a102308d38676b2ccaef78016b56a80935d))
* migrate cabinet renewal display + execute to PricingEngine ([28fc36d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/28fc36dca41b626430baf823274561269023ac59))
* migrate cart auto-purchase to PricingEngine (fresh calc) ([bd2e93a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bd2e93a6a5076341b104f7dba2b7fc5fdb587e66))
* migrate menu.py renewal pricing to PricingEngine ([652b6da](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/652b6dabde014d19075f13198dd06e5fb8bef380))
* migrate miniapp renewal display + execute to PricingEngine ([cb43aca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cb43acab3194bbf9b6e2c04ca0254ba5b2571b2d))
* migrate recurrent and monitoring services to PricingEngine ([978f68e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/978f68e7be42b0faf92d9ac5bc0bbaa2022ac95b))
* migrate remaining callers to PricingEngine + cleanup dead CRUD ([75dbd2b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/75dbd2b4fcc8ab14ac44d915bf55b10406544bb1))
* migrate try_auto_extend_expired to PricingEngine ([e6ebc67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e6ebc6722d826d291156e6bff3bf86000b32b783))
* remove dead pricing code and fix miniapp classic mode ([c9a9816](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c9a9816daa15a4534a3990822543eeefe1a1631b))
* unify first-purchase discount algorithm with PricingEngine ([fe4e6ac](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe4e6acb5391d0797ea01281eeb2e2ea59a0070f))

## [3.31.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.30.0...v3.31.0) (2026-03-12)


### New Features

* add show_in_gift toggle for tariffs in admin panel ([cb5126a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cb5126aff8c15938a59ea9c4f8e605b250b05dbc))
* add sync-squads endpoint for bulk updating subscription squads in Remnawave ([b1e2146](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b1e2146254255586b5be9bd894ac4d113a0a8cf5))
* auto-sync squads to Remnawave when admin updates tariff ([076290e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/076290e0c1d81b610a7653d6b64ed218e0f124b4))
* referral links now point to web cabinet instead of bot ([12ae871](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/12ae871653399bc4ccd23b6394878e814ce9cd75))


### Bug Fixes

* add post_update=True to User.referrals self-referential relationship ([9957259](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/995725988150f31d193631120a4692e88fa4dd57))
* add Telegram Stars payment support for gift subscriptions ([5424d8c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5424d8c31484873b0adc0bc980abdc51ee81325b))
* correct skipped_count in sync-squads circuit breaker and simplify ternary ([8a362db](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8a362db7833b5b7793b5b52345d227cb84cbc39e))
* preserve purchased devices when admin changes user tariff ([bf72f24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bf72f241d81e4432f50a61ec3bb829d18c92955d))
* prevent account takeover via auto_login_token, ensure promo group on all purchase paths ([b3f3eba](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b3f3eba5756404df9ed0f12d8048244ca536f7d3))
* reactivate subscription after traffic top-up when status is EXPIRED ([8b35428](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8b354280558a5f28d1b99eae55ccd21a4af6a07b))
* update promo group via M2M table so admin changes persist ([68bc8eb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/68bc8eb57c792059d2be8a8fff6bba3254d3773d))


### Refactoring

* remove estimated price from balance, simplify server sync, fix HTML injection ([a798f11](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a798f1143eebf52e18254bddd610f7f14a0c4056))

## [3.30.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.29.0...v3.30.0) (2026-03-11)


### New Features

* add gifts section to admin user detail API ([bca8bab](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bca8bab4336b2583da9be8c642985e6a0151e33d))
* add promo group and promo offer discounts to gift subscriptions ([2fd0f6a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2fd0f6aa4eb62f704208c1e56a6542d3967e7867))


### Bug Fixes

* record transactions for free tariff switches and admin tariff changes ([864a4ed](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/864a4ed7005195ff3be3a8bb2e7666bc5a7f3e4e))
* reset subscription for paid users, trial-to-paid tariff conversion, gift purchase MissingGreenlet ([e67b8e4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e67b8e448e5396ee6daa8c6278bb5a0b313dda74))
* use keyword args for Path.mkdir in asyncio.to_thread ([2879996](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/287999645506a49b6693a184757598e1cdceb4d8))

## [3.29.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.28.1...v3.29.0) (2026-03-10)


### New Features

* gift subscription code-only purchase + activation via deep link ([5ffce17](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ffce175dcb8aebf22cf536bfa032c66da284600))
* prevent self-activation of gift codes ([b30c73c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b30c73c300019646ea4a0d7e1bf758464ee58f0f))


### Bug Fixes

* 3 bugs — notification type, referral with channel sub, BOT_USERNAME ([3c96c2a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3c96c2affd5a803311e3c0c9a0f844d2217f387a))
* 3 critical issues from second-round review ([a90d2d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a90d2d936793daaadf116cf314b736a9ebfb7c3b))
* add minimum 8-char length check for gift token in bot deep link ([8a8337f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8a8337f538c3fcaf84b84c34b7a1e38a4ce9d580))
* address review findings from 6-agent audit ([5c34656](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5c3465647639d6f07432bc3d725bba9396af6c45))
* code-only gifts skip fulfillment in gateway webhook + retry service ([05bcac5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/05bcac502efb1b4298a1c6be91bba5d7c057b9f0))
* panel sync now updates end_date in both directions ([def594b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/def594bbb55ef45d3df81524bd8841de73a07340))
* pass full token to svc_activate instead of truncated prefix ([38c6adf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/38c6adfdb4d4fc786bf6ca34a5d54025126130c0))
* refresh user subscription after gift activation in /start ([363ccce](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/363ccce56d3e61554ca49d725322a79b05bc65d3))
* remove begin_nested that breaks activate_purchase transaction ([0005d59](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0005d59da1e58c38561c8346db89d8475a25d7df))
* stars rate rounding + device/traffic purchase stats ([641ff86](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/641ff86bf6f1ac1f22146f4344beda05759869fc))
* support prefix-based gift code lookup for activation ([4fb72ae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fb72ae6e3bc65d93ab84b594b4ff5b4856c5357))


### Refactoring

* deduplicate gift activation in start.py ([769d3a0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/769d3a0b309fb6be1c3175cd66a0df7cb6e2fb67))
* rename GIFTCODE_ start parameter prefix to GIFT_ ([42b6c80](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/42b6c80a48ad0100d5ddbbe99a096ebb7b292f08))

## [3.28.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.28.0...v3.28.1) (2026-03-10)


### Bug Fixes

* migrate pricing to days-based proration, fix promo revenue leaks, fix admin panel bugs ([fcdeff1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fcdeff1ee5155c88c634e12a703159c221d66af5))

## [3.28.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.27.0...v3.28.0) (2026-03-09)


### New Features

* add cabinet gift subscription API routes and schemas ([6a61b09](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6a61b095755885ff8973eb9ac4422740d07e0306))
* add cabinet menu layout editor with row arrangement, custom URL buttons, and drag-and-drop reordering ([dd8d7f6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dd8d7f69203490553d15dcdad6dda28fab02d593))
* add CABINET_GIFT_ENABLED branding toggle ([759bfe1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/759bfe1bdb3a3d3f917334fd32d0ea2f5be5d1f0))
* add open_in setting for custom buttons (external browser / webapp) ([497a8ee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/497a8ee5b528cf80d7042a7eec62369b6a327339))
* add source and buyer_user_id fields to GuestPurchase model ([0936d4a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0936d4a7f651a1fcef8c2f86818320af3764b423))
* implement gateway payment for gifts, persist recipient warning ([cd04f3b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cd04f3b622444f45e2edf4a92da581f3d1f79b67))


### Bug Fixes

* enforce HTTPS for webapp mode, deduplicate keyboard builder, fix long line ([69dbd6a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69dbd6a2df4cf5e0dd7156ca0f3beb53c4a061af))
* harden gift subscription feature after multi-agent review ([6a4140e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6a4140e3e203beb20cc56aa9c65dfed70f0a12d7))
* loyalty tiers current status based on spending, not assigned group ([b815abf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b815abf2b11e32eb658f9a8a63ae902bc0db46f4))
* negate GIFT_PAYMENT amounts and remove dead code ([f80b058](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f80b0583804f27c322a4eb27f0613163ca1f97e9))
* normalize threshold 0→NULL in create_promo_group for consistency ([b9089e6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b9089e693f823e3b8618d08329ccba559592dfa3))
* payment gateway issues — YooKassa polling, PAL24 card 500 ([95a32e8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/95a32e8574320eeba9276e44551a2f1207ae1e8b))
* support Telegram OIDC id_token in account linking endpoint ([680c22c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/680c22c0179253d24f7f89e115a283dac92f9a49))

## [3.27.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.26.0...v3.27.0) (2026-03-09)


### New Features

* auto-resume disabled daily subscriptions on balance topup ([770b31d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/770b31d3d05c22411b64ddbea3c304e34d879f5b))


### Bug Fixes

* add method query param to return_url and latest-payment endpoint ([32d58b0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/32d58b04b9a37473f43ae07cc32d4e18b161e3b9))
* add table existence guards to migrations for optional payment tables ([f4a7763](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f4a776319eaccbce108a1f22462da6cd592fe0f3))
* admin tariff server selection - 64-byte overflow and callback routing conflicts ([536525c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/536525c9c0a7701321bc3b83d6cef125c6f343ba))
* align tariff pricing with calculate_renewal_price reference ([6349b2f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6349b2f4426abd49e3bd63364f3d2b204a486282))
* conditional log messages and sanitize panel_error in user deletion ([289cbe9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/289cbe966e42afe74c8d1b936139941ff84e008b))
* encode payment status in provider return URLs and wire failed_url ([275f249](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/275f249bbdf28d065e1b856e4d8ec7e73af4e1aa))
* enforce tariff device_price and max_device_limit across all purchase paths ([f9f07f3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f9f07f360c36ce1eade8a27fa0fa5bf22808db93))
* keep DB session alive in Tribute payment notification handler ([4186159](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4186159a61a40003454afc9c0faf848582cfb037))
* latest-payment endpoint returns all payments, not just pending ([7a9264b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7a9264b1731cf8935c9e3985f41a1df919dfbf83))
* pass cabinet return_url to payment providers for top-up redirects ([7ca9619](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7ca96195a7240ce0c3bd613c20344d79e5219c74))
* propagate tariff squad changes to existing subscriptions and fix user deletion from Remnawave ([7ccfb66](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7ccfb66690c93df0c9c694935b16a280ca8ae812))
* renewal cost estimate double-counts servers and traffic in tariff mode ([bfbefeb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bfbefeb1e20a191f604bbcbc79b14d8c6e4cd5bd))
* resolve concurrent AsyncSession bug and sanitize error responses ([4a5cacd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4a5cacda386e7fa60ad7b6393aa3372384bee128))
* use parsed HTML length for Telegram caption limit checks ([2649e12](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2649e12f64b8f825a3db85b95da6a335b0f8eec6))


### Refactoring

* move squad propagation to service layer with parallel Remnawave sync ([79161ea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79161eaae4d67c82c45b6ea3654c0b15c8b785a4))

## [3.26.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.25.0...v3.26.0) (2026-03-08)


### New Features

* add telegram gift notification with inline activation button ([9ba61a0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ba61a08796fbc06e0dea2ee9cb02edc4126b335))


### Bug Fixes

* auto-purchase classic extend missing device_limit and traffic_limit_gb ([7dc5e4a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7dc5e4ab94a415dc739a49c74cde511aad0cbb29))
* gift purchase notification and activation flow ([330d1cb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/330d1cb6fe2eee81a3e8f841de75d41e8b4cde40))
* multiple payment and notification bugs ([f4eeb9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f4eeb9a503d6da8a152f8cb60b89f7ebbdf41c4a))
* quick topup buttons include device/server/traffic costs, broadcast button crash on media messages ([5ebe107](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ebe1072c9c8a1dcb6ee4cbbea2dc55211b534c4))
* remove is_active_paid_subscription guard from admin deactivation ([1f664a9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1f664a9083d81bc4462c30bf0626a44b5a30f03e))
* respect send_before_menu flag for pinned messages during new user registration ([20727b1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/20727b1017457769feccccf83e99523c19026a7e))

## [3.25.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.24.0...v3.25.0) (2026-03-07)


### New Features

* add configurable animated background for landing pages ([11d3e63](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11d3e637c106590a73ed804fc762bf303b37dd62))
* add landing page statistics endpoint with charts data ([25478ce](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25478ced209fdbf12f2c398ad1c8d48ac26c923e))
* add paginated purchases list endpoint for landing pages ([0ba1127](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ba112746913bb9927338a7554370ac8c4e12039))


### Bug Fixes

* add or [] guard to remaining connected_squads call site in fulfill_purchase ([d9f9f3d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9f9f3dca126963967782160766bd5b26bde7a49))
* align context_vars and SAMPLE_CONTEXTS with actual runtime context keys ([ab5313a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ab5313a381f8175346b470d6d1df54d8d7d11ff8))
* align subscription_renewed/activated context_vars with runtime keys ([c507634](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c507634398d3e934246b2c66183ebad1b9949769))
* correct device_limit and connected_squads in guest purchase fulfillment ([44d46fe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/44d46feb0adec9255fbf167e9937ea70b711e289))
* drop legacy prize_days column from contest_templates ([5214f55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5214f55f46391c7870a38ea51c2efc2f4e518f58))
* handle expired subscription in guest purchase fulfillment ([9e78509](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9e785092843cf7f6b3ebb5bfa1710030c74ceafb))
* remaining context_vars/SAMPLE_CONTEXTS mismatches found by agents ([d72ea6b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d72ea6b7f999c320038f0327a0c541d1cd276244))
* resolve alembic migration failures on fresh database install ([bbd353f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bbd353ff38af57aa9a8f15c60bded3259a3e3e26))
* resolve NameError in YooKassa successful payment processing ([9d5329d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d5329d9d1051eeaf77cfea4932557cdfbf21cc6))
* strip newlines from subject substitution, fix subscription notification context ([c9ea2b1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c9ea2b15e9d670d6e1888c0396a145713ec749c0))
* substitute context variables in email template overrides ([d52c87b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d52c87b2b752d3f432096318ac2ec4f9ad792929))
* substitute sample context in admin test email for template overrides ([351d714](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/351d714f2d4c200e8ab4263ff006ae33ec062d95))
* support {total_amount} placeholder in cart notification templates ([f4ab174](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f4ab174d32be8b48470320c27c92e75fdabd6d58))
* use --frozen instead of --locked in Dockerfile to avoid version mismatch ([923b36a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/923b36a8b9caed5db1c147a5ef4c001f66f8170a))
* use information_schema for constraint existence checks in migrations ([fc65e2d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fc65e2de4c9c08e7df1886c458b53f7a05894934))
* use pg_class lookup for constraint existence checks in migrations ([ba335fe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba335fe78430e26b9e2449dbbd6db209557698e0))

## [3.24.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.23.2...v3.24.0) (2026-03-07)


### New Features

* account linking and merge system for cabinet ([dc7b8dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc7b8dc72a3a398d6270a0a2b8ce9e2b54cb9af7))
* account merge system — atomic user merge with full FK coverage ([2664b49](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2664b4956d8436a2720d7cd5992b8cdbb72cdbd9))
* add 'default' (no color) option for button styles ([10538e7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/10538e735149bf3f3f2029ff44b94d11d48c478e))
* add admin campaign chart data endpoint with deposits/spending split ([fa7de58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fa7de589c1bd0ae37ebaaa07bae0ed3d68e01720))
* add admin notifications for partner applications and withdrawals ([cf7cc5a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cf7cc5a84e295608009f255fcd0dcedb5a2a04a3))
* add admin partner settings API (withdrawal toggle, requisites text, partner visibility) ([6881d97](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6881d97bbb1f6cd8ca3609c2d9286a6e4fb24fc3))
* add admin sales statistics API with 6 analytics endpoints ([58faf9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/58faf9eaeca63c458093d2a5e74a860f57712ab0))
* add admin topic notifications for landing page purchases ([dbb9757](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dbb9757a3c7938ab7505358942f675b82401245a))
* add all remaining RemnaWave webhook events (node, service, crm, device) ([1e37fd9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1e37fd9dd271814e644af591343cada6ab12d612))
* add button style and emoji support for cabinet mode (Bot API 9.4) ([bf2b2f1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bf2b2f1c5650e527fcac0fb3e72b4e6e19bef406))
* add cabinet admin API for pinned messages management ([1a476c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1a476c49c19d1ec2ab2cda1c2ffb5fd242288bb6))
* add campaign_id to ReferralEarning for campaign attribution ([0c07812](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c07812ecc9502f54a7745a77b086fc52bdc0e34))
* add ChatTypeFilterMiddleware to ignore group/forum messages ([25f014f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25f014fd8988b5513fba8fec4483981384687e96))
* add close button to all webhook notifications ([d9de15a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9de15a5a06aec3901415bdfd25b55d2ca01d28c))
* add daily deposits by payment method breakdown ([d33c5d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d33c5d6c07ce4a9efaf3c5aceb448e968e1b8ed7))
* add daily device purchases chart to addons stats ([2449a5c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2449a5cbbe5179a762197414a5752896383a6ee4))
* add dedicated sales_stats RBAC permission section ([8f29e2e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8f29e2eee2e0c78f7f7e87a322eaf4bd4221069c))
* add desired commission percent to partner application ([7ea8fbd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7ea8fbd584aff2127595001094ef69acb52f847f))
* add discount system for landing pages ([aa7d986](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/aa7d98630dd9be2cfb81dac3ef2c1c6730487e61))
* add external squad support for tariffs ([c10d678](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c10d6780ba89ac641769dcb0c4ab2d89f124f0b7))
* add GET /admin/rbac/users endpoint for listing all RBAC users ([8b77cda](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8b77cdae2ccc489bfead89523f31cd15bfdc675b))
* add granular user permissions (balance, subscription, promo_group, referral, send_offer) ([60c4fe2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/60c4fe2e239d8fef7726cac769711c8fcce789eb))
* add landings to permission registry ([c93dbec](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c93dbec7a0e24a6cc41449ed3c6e5fb669b127a9))
* add lite mode functionality with endpoints for retrieval and update ([7b0403a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7b0403a307702c24efefc5c14af8cb2fb7525671))
* add LOG_COLORS env setting to toggle console ANSI colors ([27309f5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/27309f53d9fa0ba9a2ca07a65feed96bf38f470c))
* add MULENPAY_WEBSITE_URL setting for post-payment redirect ([fe5f5de](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe5f5ded965e36300e1c73f25f16de22f84651ad))
* add multi-channel mandatory subscription system ([8375d7e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8375d7ecc5e54ea935a00175dd26f667eab95346))
* add partner system and withdrawal management to cabinet ([58bfaea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/58bfaeaddbcbb98cb67dbd507847a0e5c8d07809))
* add per-button enable/disable toggle and custom labels per locale ([68773b7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/68773b7e77aa344d18b0f304fa561c91d7631c05))
* add per-channel disable settings and fix CHANNEL_REQUIRED_FOR_ALL bug ([3642462](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3642462670c876052aa668c1515af8c04234cb34))
* add per-section button style and emoji customization via admin API ([a968791](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a9687912dfe756e7d772d96cc253f78f2e97185c))
* add Persian (fa) locale with complete translations ([29a3b39](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/29a3b395b6e67e4ce2437b75120b78c76b69ff4f))
* add POST /auth/telegram/oidc endpoint for OIDC popup flow ([3a400d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3a400d9f8b3b4dd2c0bb12fc68f1af6e7c880761))
* add quick purchase email templates to admin panel ([6970340](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6970340e62c67a41f3219759fb0a752617690ea0))
* add RBAC + ABAC permission system for admin cabinet ([3fee54f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3fee54f657dc6e0db1ec36697850ada2235e6968))
* add referral code tracking to all cabinet auth methods + email_templates migration ([18c2477](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/18c24771737994f3ae1f832435ed2247ca625aab))
* add RemnaWave incoming webhooks for real-time subscription events ([6d67cad](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d67cad3e7aa07b8490d88b73c38c4aca6b9e315))
* add required channels button to admin settings submenu in bot ([3af07ff](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3af07ff627fc354da4f8c41b0bd0575dddd9afa5))
* add RESET_TRAFFIC_ON_TARIFF_SWITCH admin setting ([4eaedd3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4eaedd33bf697469fe9ed6a1bfe8b59ca43b46fb))
* add resource_type and request body to audit log entries ([388fc7e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/388fc7ee67f5fc0edf6b7b64b977e12a2d8f0566))
* add separate Freekassa SBP and card payment methods ([0da0c55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0da0c5547d0648a70f848fe77c13d583f4868a52))
* add server-complete OAuth linking endpoint for Mini App flow ([f867989](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f867989557d20378cfe815c9c88e1a842c4f6654))
* add startup warnings for missing HAPP_CRYPTOLINK_REDIRECT_TEMPLATE and MINIAPP_CUSTOM_URL ([476b89f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/476b89fe8e613c505acfc58a9554d31ccf92718a))
* add sub_options support for landing page payment methods ([220196f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/220196fb7abc88b60a37c1fb60786dd3a6ada3ad))
* add Telegram account linking endpoint with security hardening ([da40d56](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da40d5662d6d064090769823d616d6f9748ab5b9))
* add Telegram OIDC id_token validation and code exchange ([2f0a9dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2f0a9dc4f3489f7d4311101191129ee95d7edbcc))
* add TELEGRAM_OIDC_* settings for new Telegram Login ([833df51](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/833df518d010d1bfd773eb0c85aaa7e653c7e153))
* add validation to animation config API ([a15403b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a15403b8b6e1ec1bb5c37fdde646e7790373e860))
* add web admin button for admins in cabinet mode ([9ac6da4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ac6da490dffa03ce823009c6b4e5014b7d2bdfb))
* add web campaign links with bonus processing in auth flow ([d955279](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9552799c17a76e2cc2118699528c5b591bd97fb))
* allow editing system roles ([f6b6e22](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f6b6e22a9528dc05b7fbfa80b63051a75c8e73cd))
* allow tariff deletion with active subscriptions ([ebd6bee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ebd6bee05ed7d9187de9394c64dfd745bb06b65a))
* attribute campaign registrations to partner for referral earnings ([767e965](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/767e9650285adc72b067b2c0b8a4d1ac5c5bba57))
* blocked user detection during broadcasts, filter blocked from all notifications ([10e231e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/10e231e52e0dbabd9195a2df373b3c95129a5e4f))
* capture query params in audit log details for all requests ([bea9da9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bea9da96d44965fcee5e2eba448960443152d4ea))
* colored channel subscription buttons via Bot API 9.4 style ([0b3b2e5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0b3b2e5dc54d8b6b3ede883d5c0f5b91791b7b9b))
* colored console logs via structlog + rich + FORCE_COLOR ([bf64611](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bf646112df02aa7aa7918d0513cb6968ceb7f378))
* configurable Telegram Login Widget with admin settings ([084a3cd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/084a3cd16f8825c389514813ba679748ba235d0a))
* enforce 1-to-1 partner-campaign binding with partner info in campaigns ([366df18](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/366df18c547047a7c69192c768970ebc6ee426fc))
* enhance sales stats with device purchases, per-tariff daily breakdown, and registration tracking ([31c7e2e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/31c7e2e9c14cb88762a62a72e4f65051e0c6c1fd))
* expose oidc_enabled and oidc_client_id in telegram-widget config ([000b0c0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/000b0c0592773d0a5f6f572fd8a721ce0f474b2c))
* expose payment sub-options with labels in public landing API ([c53e9af](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c53e9af744114e5d6fe014b09b4fac8da1e59c6e))
* expose traffic_reset_mode in subscription response ([59383bd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/59383bdbd8c72428d151cb24d132452414b14fa3))
* expose traffic_reset_mode in tariff API response ([5d4a94b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5d4a94b8cea8f16f0b4c31e24a4695bee4c67af7))
* guest purchase → cabinet account integration ([f8edfd7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f8edfd77463aad64d9e616569467b4883be4dccf))
* guest purchase delivery & activation system ([776fc3a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/776fc3aadc14e1cc415286cf008fa4eb85f21164))
* handle errors.bandwidth_usage_threshold_reached_max_notifications webhook ([8e85e24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8e85e244cb786fb4c06162f2b98d01202e893315))
* handle service.subpage_config_changed webhook event ([43a326a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/43a326a98ccc3351de04d9b2d660d3e7e0cb0efc))
* include partner campaigns in /partner/status response ([ea5d932](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ea5d932476553ad1750da3bebbd4b8f055478040))
* link campaign registrations to partner for referral earnings ([c4dc43e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c4dc43e054e9faec2f9614fe51a64635f80c1796))
* **localization:** add Persian (fa) locale support and wire it across app flows ([cc54a7a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cc54a7ad2fb98fe6e662e1923027f4989ae72868))
* notify users on partner/withdrawal approve/reject ([327d4f4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/327d4f4d1559e37dc591adbfd0c839d986d1068d))
* register TELEGRAM_OIDC category, hints in admin settings ([3a36162](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3a361628aa543cb629d6967d84d7f474b89c3841))
* rename MAIN_MENU_MODE=text to cabinet with deep-linking to frontend sections ([ad87c5f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ad87c5fb5e1a4dd0ef7691f12764d3df1530f643))
* replace pip with uv in Dockerfile ([e23d69f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e23d69fcec7ab65a14b054fd46f6ecf87ae6fd13))
* rework guide mode with Remnawave API integration ([5a269b2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5a269b249e8e6cad266822095676937481613f5f))
* show all active webhook endpoints in startup log ([9d71005](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d710050ad40ba76a14aa6ace8e8a47f25cdde94))
* unified notification delivery for webhook events (email + WS support) ([26637f0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/26637f0ae5c7264c0430487d942744fd034e78e8))
* webhook protection — prevent sync/monitoring from overwriting webhook data ([184c52d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/184c52d4ea3ce02d40cf8a5ab42be855c7c7ae23))
* мультиязычные лендинги + гостевые платежи для всех провайдеров ([6deab7d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6deab7dd8c5c5df812bd69608369258a10a67ca4))
* публичные лендинг-страницы для быстрой покупки VPN-подписок ([5e404cc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5e404cc082859d875f988911fcc4eedaa35b886b))


### Bug Fixes

* 3 user deletion bugs — type cast, inner savepoint, lazy load ([af31c55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/af31c551d2f23ef01425bdb2db8f255dbc3047e2))
* abs() for transaction amounts in admin notifications and subscription events ([fd139b2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fd139b28a2c45cc3fbd2e01707fb83fbabf57c71))
* add /start burst rate-limit to prevent spam abuse ([61a9722](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/61a97220d30031816ab23e33a46717e4895c0758))
* add abs() to expenses query, display flip, contest stats, and recent payments ([de6f806](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/de6f80694ba8aa240764e2769ec04c16fe7f3672))
* add action buttons to webhook notifications and fix empty device names ([7091eb9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7091eb9c148aaf913c4699fc86fef5b548002668))
* add activate hint to gift pending activation email link ([fa21549](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fa21549cac9098f49e2e32868acce461acd1b40d))
* add blocked_count column migration to universal_migration.py ([b4b10c9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b4b10c998cadbb879540e56dbd0e362b5497ee57))
* add diagnostic logging for device_limit sync to RemnaWave ([97b3f89](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97b3f899d12c4bf32b6229a3b595f1b9ad611096))
* add exc_info traceback to sync user error log ([efdf2a3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/efdf2a3189a2f790e570f9a6e19d91469be4ea4f))
* add int32 overflow guards and strengthen auth validation ([50a931e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/50a931ec363d1842126b90098f93c6cae47a9fac))
* add IntegrityError handling on link commit and format fixes ([0c1dc58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c1dc580c67254d11ffb096c22d8c8d78ac18e2b))
* add local traffic_used_gb reset in all tariff switch handlers ([2cdbbc0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cdbbc09ba9a19dcb720049ffde08ba780ac5751))
* add Message-ID and Date headers to outgoing emails ([de541ea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/de541ea1c3fa20c606c0ea1b69a0223569afb9e2))
* add Message-ID and Date headers to outgoing emails ([e9b4d8e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e9b4d8e444be9ab666caf642c849dcf63b1884ab))
* add migration for partner system tables and columns ([4645be5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4645be53cbb3799aa6b2b6a623af30460357a554))
* add migration for partner system tables and columns ([79ea398](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79ea398d1db436a7812a799bf01b2c1c3b1b73be))
* add min_length to state field, use exc_info for referral warning ([062c486](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/062c4865db194f9d2242772044402fa2711a69bd))
* add missing broadcast_history columns and harden subscription logic ([d4c4a8a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d4c4a8a211eaf836024f8d9dcb725f25f514f05e))
* add missing CHANNEL_CHECK_NOT_SUBSCRIBED localization key ([a47ef67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a47ef67090c4e48f466286f7c676eeee0c61a4fb))
* add missing mark_as_paid_subscription, fix operation order, remove dead code ([5f2d855](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f2d855702dea838b38887a5f44b9ad759acd5cf))
* add missing payment providers to payment_utils and fix {total_amount} formatting ([bdb6161](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bdb61613de378efab4de6de98fde2de3b554c548))
* add missing placeholders to Arabic SUBSCRIPTION_INFO template ([fe54640](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe546408857128649930de9473c7cde1f7cc450a))
* add missing subscription columns migration ([b96e819](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b96e819da4cc37710e9fc17467045b33bcffac4d))
* add naive datetime guards to fromisoformat() in Redis cache readers ([1b3e6f2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b3e6f2f11c20aa240da1beb11dd7dfb20dbe6e8))
* add naive datetime guards to fromisoformat() in Redis cache readers ([6fa4948](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6fa49485d9f1cd678cb5f9fa7d0375fd47643239))
* add naive datetime guards to parsers and fix test datetime literals ([0946090](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/094609005af7358bf5d34d252fc66685bd25751c))
* add passive_deletes to Subscription relationships to prevent NOT NULL violation on cascade delete ([bfd66c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bfd66c42c1fba3763f41d641cea1bd101ec8c10c))
* add pending_activation to purchase stats and show total count ([8510597](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8510597ddb501c479d5b70118a94944556ab984f))
* add promo code anti-abuse protections ([97ec39a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97ec39aa803f0e3f03fdcd482df0cbcb86fd1efd))
* add referral_code pattern validation, email login rate limiting, and Retry-After headers ([5499ad6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5499ad62dc98346bef9cb83bf6d8bca319291371))
* add selectinload for campaign registrations in list query ([4d74afd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4d74afd7118524623371f904a93ae1fcbba8d64e))
* add selectinload for subscription in campaign user list ([eb9dba3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eb9dba3f4728b478f2206ff992700a9677f879c7))
* add startup warning for missing HAPP_CRYPTOLINK_REDIRECT_TEMPLATE in guide mode ([1d43ae5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1d43ae5e25ffcf0e4fe6fec13319d393717e1e50))
* add X-CSRF-Token and X-Telegram-Init-Data to CORS allow_headers ([77456ef](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/77456efb7504e12c9b9879a352118ce1687132b1))
* address code review findings for Telegram OIDC ([da1cc4f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da1cc4fe5ab6436210185a12dc2a82cb153fc24a))
* address code review issues in guide mode rework ([fae6f71](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fae6f71def421e319733e4edcf1ca80a2831b2ec))
* address RBAC review findings (CRITICAL + HIGH) ([1646f04](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1646f04bde47a08f3fd782b7831d40760bd1ba60))
* address remaining abs() issues from review ([ff21b27](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ff21b27b98bb5a7517e06057eb319c9f3ebb74c7))
* address review findings for guest purchase admin notifications ([770f19e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/770f19e84688e55ed44f7c9de26b0e9ae9636c4b))
* address review findings from agent verification ([cc5be70](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cc5be7059fdf4cefb01e97196c825b217f8b54b3))
* address review issues in backup, updates, and webhook handlers ([2094886](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/20948869902dc570681b05709ac8d51996330a6e))
* address security review findings ([6feec1e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6feec1eaa847644ba3402763a2ffefd8f770cc01))
* align RBAC route prefixes with frontend API paths ([5a7dd3f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5a7dd3f16408f3497a9765e79a540ccdabc50e69))
* allow email change for unverified emails ([93bb8e0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/93bb8e0eb492ca59e29da86594e84e9c486fea65))
* allow non-HTTP deep links in crypto link webhook updates ([f779225](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f77922522a85b3017be44b5fc71da9c95ec16379))
* allow purchase when recalculated price is lower than cached ([19dabf3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/19dabf38512ae0c2121108d0b92fc8f384292484))
* allow tariff switch when less than 1 day remains ([67f3547](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/67f3547ae2f40153229d71c1abe7e1213466e5c3))
* always include details in successful audit log entries ([3dc0b93](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3dc0b93bdfc85fb97f371dc34e024272766afc65))
* AttributeError in withdrawal admin notification (send_to_admins → send_admin_notification) ([c75ec0b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c75ec0b22a3f674d3e1a24b9d546eca1998701b3))
* auth middleware catches all commit errors, not just connection errors ([6409b0c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6409b0c023cd7957c43d5c1c3d83e671ccaf959c))
* auto-convert naive datetimes to UTC-aware on model load ([f7d33a7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f7d33a7d2b31145a839ee54676816aa657ac90da))
* auto-update permissions for system roles on bootstrap ([eff74be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eff74bed5bcc47a6cfa05c20cad14a40c1572d1f))
* backup restore fails on FK constraints and transaction poisoning ([ff1c872](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ff1c8722c9188fdbaf765d6b7e9192686df64850))
* build composite device name from platform + hwid short suffix ([17ce640](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/17ce64037f198837c8f2aa7bf863871f60bdf547))
* callback routing safety and cache invalidation order ([6a50013](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6a50013c21de199df0ba0dab3600b693548b6c1e))
* campaign web link uses ?campaign= param, not ?start= ([28f524b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/28f524b7622ed975d2fece66edc94d9713354738))
* cap expected_monthly_referrals to prevent int32 overflow ([2ef6185](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2ef618571570edb6011a365af8aa9cd7e3348c2e))
* centralize balance deduction and fix unchecked return values ([0466528](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0466528925a24087b8522a10cbb11c947c2b7d91))
* centralize has_had_paid_subscription into subtract_user_balance ([e4a6aad](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e4a6aad621be7ef4e7aedb21373927ede0c8d0a5))
* change CryptoBot URL priority to bot_invoice_url for Telegram opening ([3193ffb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3193ffbd1bee07cb79824d87cb0f77b473b22989))
* classic mode prices overridden by active tariff prices ([628a99e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/628a99e7aa0812842dabc430857190c0cd5c2680))
* clean email verification and password fields from secondary user during merge ([7b4e948](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7b4e9488f6fbd1271f063579e48ca9a3c96cb645))
* clean stale squad UUIDs from tariffs during server sync ([fcaa9df](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fcaa9dfb27350ceda3765c6980ad67f671477caf))
* clear subscription data when user deleted from Remnawave panel ([b0fd38d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b0fd38d60c22247a0086c570665b92c73a060f2f))
* close remaining daily subscription expire paths ([618c936](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/618c936ac9ce4904cd784bf2278d3da188895f2d))
* code style and formatting from review ([a539d69](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a539d698546a60aa0a06759f91c77476380a20b1))
* complete datetime.utcnow() → datetime.now(UTC) migration ([eb18994](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eb18994b7d34d777ca39d3278d509e41359e2a85))
* complete FK migration — add 27 missing constraints, fix broadcast_history nullable ([fe393d2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe393d2ca6ce302d8213cc751842ea92ef277e76))
* comprehensive security and quality fixes from 7-agent review ([5c55662](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5c55662e2c7068456aeee435b543a851225ff39e))
* comprehensive security hardening from 7-agent review ([e96fe1e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e96fe1ecd8d90878a3fbad9ed76c1a2e7f3a1415))
* connected_squads stores UUIDs, not int IDs — use get_server_ids_by_uuids ([d7039d7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7039d75a47fbf67436a9d39f2cd9f65f2646544))
* consume promo offer in miniapp tariff-mode renewal path ([b8857e7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b8857e789ef60cf0c8766abbeadd094f62070a61))
* consume promo offer in tariff_purchase.py, fix negative transaction amount ([c8ef808](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c8ef80853915af3e3eb254edd07d8d78b66a9282))
* correct broadcast button deep-links for cabinet mode ([e5fa45f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e5fa45f74f969b84f9f1388f8d4888d22c46d7e8))
* correct cart notification after balance top-up ([2fab50c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2fab50c340c885fc92a4bf797a4b03da6e44af31))
* correct referral withdrawal balance formula and commission transaction type ([83c6db4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/83c6db48349440447305604e944fa440bdceb3fb))
* correct subscription_service import in broadcast cleanup ([6c4e035](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6c4e035146934dffb576477cc75f7365b2f27b99))
* count sales from completed payment transactions instead of subscription created_at ([06c3996](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/06c3996da4fa14eafb294651158068c7cda51e52))
* critical OIDC fixes from 7-agent review ([b78c01c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b78c01cae9746275057aaf61c0876ccfd72e1f62))
* critical security and data integrity fixes for partner system ([8899749](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/88997492c3534ea2f6e194c0382c77302557c2f3))
* cross-validate Telegram identity on every authenticated request ([973b3d3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/973b3d3d3ff80376c0fd19c531d7aac3ae751df8))
* CryptoBot guest payment — remove is_paid [@property](https://github.com/property) write, use correct status ([6f871ed](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6f871edc9d01ca20d1b194a157d3d6ae46512d05))
* daily tariff subscriptions stuck in expired/disabled with no resume path ([80914c1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/80914c1af739aa0ee1ea75b0e5871bf391b9020d))
* deadlock on user deletion + robust migration 0002 ([b7b83ab](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b7b83abb723913b3167e7462ff592a374c3f421b))
* delete cross-referral earnings before bulk reassignment, clear secondary.referred_by_id ([f204b67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f204b678803297ce60faad628d16f46344b11ed0))
* delete subscription_servers before subscription to prevent FK violation ([7d9ced8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7d9ced8f4f71b43ed4ac798e6ff904a086e1ac4a))
* device_limit fallback 1→0 для корректного отображения безлимита ([3e26832](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3e26832e745368a0dab2617e4e8ae2c410c6bca2))
* don't delete Heleket invoice message on status check ([9943253](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/994325360ca7665800177bfad8f831154f4d733f))
* downgrade Telegram timeout errors to warning in monitoring service ([e43a8d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e43a8d6ce4c40a7212bf90644f82da109717bdcb))
* downgrade transient API errors (502/503/504) to warning level ([ec8eaf5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ec8eaf52bfdc2bde612e4fc0324575ba7dc6b2e1))
* eliminate deadlock by matching lock order with webhook ([d651a6c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d651a6c02f501b7a0ded570f2db6addcc16173a9))
* eliminate double panel API call on tariff change, harden cart notification ([b2cf4aa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b2cf4aaa91f3fb63dca7e70645cadb75aa158cfe))
* eliminate referral system inconsistencies ([60c97f7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/60c97f778bc4cc18aaf4d8a31826bc831c3b3f8f))
* email verification bypass, ban-notifications size limit, referral balance API ([256cbfc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/256cbfcadfd2fc88d8de69557c78618639af157d))
* empty JSONB values exported as None in backup ([57aaca8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57aaca82f5bf9d7bdd9d4b924aa3412d85eccbb5))
* enforce user restrictions in cabinet API and fix poll history crash ([faba3a8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/faba3a8ed6d428305f9ca7d7fd9bdcc1fd72ba52))
* expand backup coverage to all 68 models and harden restore ([02e40bd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/02e40bd6f7ef8e653cae53ccd127f2f79009e0d4))
* extend naive datetime guard to all model properties ([bd11801](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bd11801467e917d76005d1a782c71f5ae4ffee6e))
* extract device name from nested hwidUserDevice object ([79793c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79793c47bbbdae8b0f285448d5f70e90c9d4f4b0))
* extract real client IP from X-Forwarded-For/X-Real-IP headers ([af6686c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/af6686ccfae12876e867cdabe729d0c893bd85a1))
* filter out traffic packages with zero price from purchase options ([64a684c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/64a684cd2ff51e663a1f70e61c07ca6b4f6bfc91))
* flood control handling in pinned messages and XSS hardening in HTML sanitizer ([454b831](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/454b83138e4db8dc4f07171ee6fe262d2cd6d311))
* force basicConfig to replace pre-existing handlers ([7eb8d4e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7eb8d4e153bab640a5829f75bfa6f70df5763284))
* freekassa OP-SP-7 error and missing telegram notification ([200f91e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/200f91ef1748bb6213d1ef3a8e83ae976290a8a7))
* from redis.exceptions import NoScriptError ([667291a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/667291a2dcaeae21e27eeb6376085e69caa4e45a))
* generate missing crypto link on the fly and skip unresolved templates ([4c72058](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4c72058d4ad8b0594991b17323928d9004803bfa))
* grant legacy config-based admins full RBAC access ([8893fc1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8893fc128e3d8927054f1df1647e896e780c69e7))
* handle duplicate remnawave_uuid on email sync ([eaeee7a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eaeee7a765c03ff33e2928cdb41be91948eca95c))
* handle expired callback queries and harden middleware error handling ([f52e6ae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f52e6aedac3de1c9bb2ad1a5a16b06d38b79ab63))
* handle expired ORM attributes in sync UUID mutation ([9ae5d7b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ae5d7bb60c57e2c29d6f3c5098c23450d5feb61))
* handle naive datetime in raw SQL row comparison (payment/common) ([38f3a9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/38f3a9a16a24e85adf473f2150aad31574a87060))
* handle naive datetimes in Subscription properties ([e512e5f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e512e5fe6e9009992b5bc8b9be7f53e0612f234a))
* handle NULL used_promocodes for migrated users ([cdcabee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cdcabee80d1d7f0b367a97cdec20bb49e8592115))
* handle nullable traffic_limit_gb and end_date in subscription model ([e94b93d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e94b93d0c10b4e61d7750ca47e1b2f888f5873ed))
* handle photo message in ticket creation flow ([e182280](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e1822800aba3ea5eee721846b1e0d8df0a9398d1))
* handle RemnaWave API errors in traffic aggregation ([ed4624c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ed4624c6649bdbc04bc850ef63e5c86e26a37ce4))
* handle StaleDataError in webhook user.deleted server counter decrement ([c30c2fe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c30c2feee1db03f0a359b291117da88002dd0fe0))
* handle StaleDataError in webhook when user already deleted ([d58a80f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d58a80f3eaa64a6fc899e10b3b14584fb7fc18a9))
* handle tariff_extend callback without period (back button crash) ([ba0a5e9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba0a5e9abd9bd582968d69a5c6e57f336094c782))
* handle TelegramBadRequest in ticket edit_message_text calls ([8e61fe4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8e61fe47746da2ac09c3ea8c4dbfc6be198e49e3))
* handle time/date types in backup JSON serialization ([27365b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/27365b3c7518c09229afcd928f505d0f3f66213f))
* handle unique constraint conflicts during backup restore without clear_existing ([5893874](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/589387477624691e0026086800428e7e52e06128))
* handle YooKassa NotFoundError gracefully in get_payment_info ([df5b1a0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/df5b1a072d99ff8aee0c94304b2a0214f0fcffe7))
* harden account merge security and correctness ([d855e9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d855e9e47fab1a038e581437a9921bdfeb11e927))
* harden backup create/restore against serialization and constraint errors ([fc42916](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fc42916b10bb698895eb75c0e2568747647555d3))
* hide traffic topup button when tariff doesn't support it ([399ca86](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/399ca86561f4271e9c542bac87c0dd2931a223e0))
* HTML parse fallback, email change race condition, username length limit ([d05ff67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d05ff678abfacaa7e55ad3e55f226d706d32a7b7))
* HTML-escape all externally-sourced text in guide messages ([711ec34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/711ec344c646844401f355695a7e8c0d4fb401ee))
* ignore 'message is not modified' on privacy policy decline ([be1da97](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/be1da976e14a35e6cca01a7fca7529c55c1a208b))
* improve campaign notifications and ticket media in admin topics ([a594a0f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a594a0f79f48227f75d6102b4586179102c4d344))
* improve campaign routes, schemas, and add database indexes ([ded5c89](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ded5c899f7425707b17fef4d0d5ceafac777ef08))
* improve deduplication log message wording in monitoring service ([2aead9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2aead9a68b6bf274c8d1497c85f2ed4d4fc9c70b))
* include desired_commission_percent in admin notification ([dc3d22f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc3d22f52db40150d595bccf524d38790e5725d9))
* initialize logger in bot_configuration.py ([988d0e5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/988d0e5c2f27538135d757187a0b6770f078b1d9))
* invalidate app config cache on local file saves ([978726a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/978726a7856cf56257c49491afe569fa8c395eac))
* limit Rich traceback output to prevent console flood ([11ef714](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11ef714e0dde25a08711c0daeee943b6e71e20b7))
* make migration 0002 robust with table existence checks ([f076269](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f076269c323726c683a38db092d907591a26e647))
* make migrations 0010/0011 idempotent, escape HTML in crash notification ([a696896](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a696896d2c4a3d0d6026398fcdc76ded9575375d))
* make users.promo_group_id nullable — sync DB with model ([e0f2243](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e0f2243f49ca8cc741a5c07b63ef3eb2abdef52c))
* medium-priority fixes for partner system ([7c20fde](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7c20fde4e887749d72280a8804467645e5bab416))
* **merge:** validate before consuming token, add flush, defensive balance ([bc1e6fb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc1e6fb22c6e23c7a34364796f51a55c60224aff))
* migrate all remaining naive timestamp columns to timestamptz ([708bb9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/708bb9eec7ea4360b26709fb2a3f82dd139ed600))
* migrate VK OAuth to VK ID OAuth 2.1 with PKCE ([1dfa780](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1dfa78013c4fb926a2b32bf4d63baa28215e7340))
* MissingGreenlet on campaign registrations access ([018f18f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/018f18fa0c9bba1a1dbca8b2398b9611d0c94c36))
* move PartnerStatus enum before User class to fix NameError ([acc1323](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/acc1323a542b8e92433cabf1334d2d98bfa21e21))
* NameError in set_user_devices_button — undefined action_text ([1b8ef69](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b8ef69a1bbb7d8d86827cf7aaa4f05cbf480d75))
* negative balance transfer, linking state validation, referrer migration ([531d5cf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/531d5cff3019e72dde6ee64977cb801e8f8c8d0b))
* normalize transaction amount signs across all aggregations ([4247981](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4247981c98111af388c98628c1e61f0517c57417))
* nullify payment FK references before deleting transactions in user restoration ([0b86f37](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0b86f379b4e55e499ca3d189137e2aed865774b5))
* partner system — CRUD nullable fields, per-campaign stats, atomic unassign, diagnostic logging ([ed3ae14](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ed3ae14d0c378fa0dc2d442c3aa5a70172f3132c))
* pass return_url to all payment providers for guest purchases ([b85646a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b85646af85c4b2036f1c07c89e3e282f74d43c1e))
* payment race conditions, balance atomicity, renewal rollback safety ([c5124b9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c5124b97b63eda59b52d2cbf9e2dcdaa6141ed6e))
* photo handling in QR messages ([1afcd84](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1afcd84e0ed2c39abd674170b8b17e6c7ee8754d))
* pre-existing bugs found during review ([1bb939f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1bb939f63a360a687fafba26bc363024df0f6be0))
* pre-validate CABINET_BUTTON_STYLE to prevent invalid values from suppressing per-section defaults ([46c1a69](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/46c1a69456036cb1be784b8d952f27110e9124eb))
* preserve connected_squads during subscription replacement cleanup ([d86c29a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d86c29a5d384db1d11ef3666153fa288d0c822d8))
* preserve payment initiation time in transaction created_at ([90d9df8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/90d9df8f0e949913f09c4ebed8fe5280453ab3ab))
* preserve purchased traffic when extending same tariff ([b167ed3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b167ed3dd1c6e6239db2bdbb8424bcb1fb7715d9))
* prevent 'caption is too long' error in logo mode ([6e28a1a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6e28a1a22b02055b357051dfecbee7fefbebc774))
* prevent cascading greenlet errors after sync rollback ([a1ffd5b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a1ffd5bda6b63145104ce750835d8e6492d781dc))
* prevent concurrent device purchases exceeding max device limit ([1cfede2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1cfede28b7570bcaf77cb53d6b2a9f3b0e4e9408))
* prevent daily subscriptions from being expired by middleware/CRUD/webhook ([0ed6397](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ed6397fa9e5810fcffc9152ab2241fcf37cf85a))
* prevent fileConfig from destroying structlog handlers ([e78b104](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e78b1040a50ac14759bceab396d0c3e34dd79cdd))
* prevent infinite reuse of first_purchase_only promo code discounts ([2cec8dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cec8dc4a487017f4b1c5ca80710f2d70045b825))
* prevent negative amounts in spent display and balance history ([c30972f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c30972f6a7911a89a6c3f2080019ff465d11b597))
* prevent partner self-referral via own campaign link ([115c0c8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/115c0c84c0698591da75d7d3b8fbd8e0fc8541ea))
* prevent race condition expiring active daily subscriptions ([bfef7cc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bfef7cc6296e296f17068e519469c3deaddc1b3b))
* prevent self-referral loops, invalidate all sessions on merge ([db61365](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/db61365e11ccec4dd45671b33da00f4b05484589))
* prevent squad drop on admin subscription type change, require subscription for wheel spins ([59f0e42](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/59f0e42be7e3c679d15cf2fc6820ab7097cd2201))
* prevent sync from overwriting end_date for non-ACTIVE panel users ([49871f8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/49871f82f37d84979ea9ec91055e3f046d5854be))
* prevent sync from overwriting subscription URLs with empty strings ([9c00479](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9c004791f28fbcf314b93c1b2a38593069605239))
* promo code max_uses=0 conversion and trial UX after promo activation ([1cae713](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1cae7130bc87493ab8c7691b3c22ead8189dab55))
* protect active paid subscriptions from being disabled in RemnaWave ([1b6bbc7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b6bbc7131341b4afd739e4195f02aa956ead616))
* protect server counter callers and fix tariff change detection ([bee4aa4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bee4aa42842b8b6611c7c268bcfced408a227bc0))
* RBAC API response format fixes and audit log user info ([4598c27](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4598c2785a42773ee8be04ada1c00d14824e07e0))
* RBAC audit log action filter and legacy admin level ([c1da8a4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c1da8a4dba5d0c993d3e15b2866bdcfa09de1752))
* read discount overrides from landing model instead of response DTO ([6d65e15](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d65e152669a7e92f93f592993c1d5507b890046))
* read OIDC enabled setting from DB in auth endpoint ([2405dc5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2405dc5c1b6d6266da373e0e4dac6444b0e70a03))
* reassign orphaned records on merge, eliminate TOCTOU race ([d7a9d2b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7a9d2bfba5b796882d3e04be6038b766cd0a4c8))
* redis cache uses sync client due to import shadowing ([667291a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/667291a2dcaeae21e27eeb6376085e69caa4e45a))
* reject promo codes for days when user has no subscription or trial ([e32e2f7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e32e2f779d014d587b58d63b513fd913ae1b7a41))
* remove [@username](https://github.com/username) channel ID input, auto-prefix -100 for bare digits ([a7db469](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7db469fd7603e7d8dac3076f5d633da654a3a57))
* remove decorative cloudpayments sub-options ([694aecc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/694aeccc3121116bf193b5766572de7472eb4016))
* remove DisplayNameRestrictionMiddleware ([640da34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/640da3473662cfdcceaa4346729467600ac3b14f))
* remove executable bit from email_service.py ([372d628](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/372d628908294d905c37828219cac6aef7941151))
* remove gemini-effect and noise from allowed background types ([731eb24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/731eb2436428d0e12f1e5ccdebc72cd74fd7c65e))
* remove local UTC re-imports shadowing module-level import in purchase.py ([e68760c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e68760cc668016209f4f19a2e08af8680343d6ed))
* remove premature tariff_id assignment in _apply_extension_updates ([b47678c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b47678cfb0ba5897b37dfe1f94e3d1336af5698e))
* remove redundant trial inactivity monitoring checks ([d712ab8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d712ab830166cab61ce38dd32498a8a9e3e602b0))
* remove subscription connection links from guest purchase emails ([9217352](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9217352685189118620f1246bc7d7a4459883ed6))
* remove unused PaymentService from MonitoringService init ([491a7e1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/491a7e1c425a355e55b3020e2bcc7b96047bdf5e))
* renewals stats empty on all-time filter ([e25fcfc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e25fcfc6ef941465b83f368f152304ea5a6747d9))
* reorder button_click_logs migration to nullify before ALTER TYPE ([df5415f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/df5415f30b2aae4412ff5fbd3cac8076128b818c))
* repair missing DB columns and make backup resilient to schema mismatches ([c20355b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c20355b06df13328f85cc5a6045b3e490419a30a))
* replace deprecated Query(regex=) with pattern= ([871ceb8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/871ceb866ccf1f3a770c7ef33406e1a43d0a7ff7))
* reset QR photo when returning to referral ([3ee108f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3ee108fce85962dde5bc6c80b3464278369da9f5))
* reset traffic purchases on expired subscription renewal + pricing fixes ([dce9eaa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dce9eaa5971cb1dc0945747e02397a250e8e411b))
* resolve deadlock on server_squads counter updates and add webhook notification toggles ([57dc1ff](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57dc1ff47f2f6183351db7594544a07ca6f27250))
* resolve exc_info for admin notifications, clean log formatting ([11f8af0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11f8af003fc60384abafa2b670b89d6ad3ac57a4))
* resolve GROUP BY mismatch for daily_by_tariff query ([e5f29eb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e5f29eb041e88bc6315f0b4da3b78898d9dd7fff))
* resolve HIGH-priority performance and security issues in partner system ([fcf3a2c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fcf3a2c8062752b2b1dc06b5993ac2d8ae80ee85))
* resolve MissingGreenlet error when accessing subscription.tariff ([a93a32f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a93a32f3a7d1b259a2e24954ae5d2b7c966c5639))
* resolve ruff lint errors (import sorting, unused variable) ([b2d7abf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b2d7abf5bd10a98fd7ad1da50b5072afc65a5b48))
* resolve sync 404 errors, user deletion FK constraint, and device limit not sent to RemnaWave ([1ce9174](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1ce91749aa12ffcefcf66bea714cea218739f3fe))
* restore merge token on DB failure, fix partner_status priority ([9582758](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9582758d1c85735c8ead8cbfeb56bbdae45288af))
* restore panel user discovery on admin tariff change, localize cart reminder ([1256ddc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1256ddcd1a772f90e7bdf9437043a47ea9d84d53))
* restore RemnaWave config management endpoints ([6f473de](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6f473defef32a6d81cee55ef2cd397d536a784a7))
* restore subscription_url and crypto_link after panel sync ([26efb15](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/26efb157e476a18b036d09167628a295d7e4c10b))
* return zeroed stats dict when withdrawal is disabled ([7883efc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7883efc3d6e6d8bedf8e4b7d72634cbab6e2f3d7))
* review findings — exception chaining, redundant unquote, validator tightening ([467dea1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/467dea1315fbaf8d09ccbba292cd0bcc60d9f3ab))
* safe HTML preview truncation and lazy-load subscription fallback ([40d8a6d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/40d8a6dc8baf3f0f7c30b0883898b4655a907eb5))
* second round review fixes for account merge ([64ee045](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/64ee0459e4e3d3fe87ad65387fcbcb147147ac1b))
* security and architecture fixes for webhook handlers ([dc1e96b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc1e96bbe9b4496e91e9dea591c7fc0ef4cc245b))
* separate base and purchased traffic in renewal pricing ([739ba29](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/739ba2986f41b04058eb14e8b87b0699fe96f922))
* show negative amounts for withdrawals in admin transaction list ([5ee45f9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ee45f97d179ce2d32b3f19eeb6fd01989a30ca7))
* skip blocked users in trial notifications and broadcasts without DB status change ([493f315](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/493f315a65610826a04e04c3d2065e0b395426ed))
* skip users with active subscriptions in admin inactive cleanup ([e79f598](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e79f598d17ffa76372e6f88d2a498accf8175c76))
* specify foreign_keys on User.admin_roles_rel to resolve ambiguous join ([bc7d061](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc7d0612f1476f2fdb498cd76a9374b41fd9440a))
* stack promo group + promo offer discounts in bot (matching cabinet) ([628997f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/628997fb48413cc4fae9ac491d1c7f6185877200))
* stop CryptoBot webhook retry loop and save cabinet payments to DB ([2cb6d73](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cb6d731e96cbfc305b098d8424b84bfd6826fb4))
* suppress 'message is not modified' error in updates panel ([3a680b4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3a680b41b0124848572809d187cab720e1db8506))
* suppress bot-blocked-by-user error in AuthMiddleware ([fda9f3b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fda9f3beecbfcca4d7abc16cf661d5ad5e3b5141))
* suppress expired callback query error in AuthMiddleware ([2de4384](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2de438426a647e2bcae9b4d99eef4093ff8b5429))
* suppress startup log noise (~350 lines → ~30) ([8a6650e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8a6650e57cd8ea396d9b057a7753469947f38d29))
* suppress web page preview when logo mode is disabled ([1f4430f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1f4430f3af8f3efcc58ef7b562904adcb1640a44))
* sync subscription status from panel in user.modified webhook ([5156d63](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5156d635f0b5bc0493e8f18ce9710cca6ff4ffc8))
* sync support mode from cabinet admin to SupportSettingsService ([516be6e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/516be6e600a08ad700d83b793dc64b2ca07bdf44))
* sync SUPPORT_SYSTEM_MODE between SystemSettings and SupportSettings ([0807a9f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0807a9ff19d1eb4f1204f7cbeb1da1c1cfefe83a))
* sync traffic reset across all tariff switch code paths ([d708365](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d708365aca9dfd5c3afda1a1de4303e0bd1d263e))
* sync uv.lock version with pyproject.toml 3.23.1 ([8eb6a8c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8eb6a8c4606a0ea48e383c031ad83219fc8e062b))
* sync uv.lock version with pyproject.toml 3.23.1 ([bc52fd2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc52fd27113f95a4154b1990142d46ae606fd2e0))
* ticket creation crash and webhook PendingRollbackError ([760c833](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/760c833b7402541d3c7cf2ed7fc0418119e75042))
* traceback in Telegram notifications + reduce log padding ([909a403](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/909a4039c43b910761bd05c36e79c8e6773199db))
* transaction boundary and CORS in webapi ([6495384](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6495384bcfd76c377971438f6c132f1404ea1f7d))
* translate required channels handler to Russian, add localization keys ([1bc9074](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1bc9074c1bcdaba7215065c77aac9dd51db4d7c8))
* treat empty icon_url as None in payment method validation ([ab981dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ab981dce0d84bba3df5fc4366e39ba3ed0adeccd))
* unassign all campaigns when revoking partner status ([d39063b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d39063b22ffb6442e275db39704361cdb9251793))
* UnboundLocalError for get_logo_media in required_sub_channel_check ([d3c14ac](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d3c14ac30363839d1340129f279a7a7b4b021ed1))
* UniqueViolation при мерже аккаунтов с общим OAuth/telegram/email ID ([1c89bd8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1c89bd8b2acfe49de2c97dd75446a037a54fded7))
* uploaded backup restore button not triggering handler ([ebe5083](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ebe508302b906f8b56cb230b934fb8566990c684))
* use .is_(True) and add or 0 guards per code review ([69b5ca0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69b5ca06701e7381c39448e2bf6b927f0558058c))
* use actual DB columns for subscription fallback query ([f0e7f8e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f0e7f8e3bec27d97a3f22445948b8dde37a92438))
* use aiogram 3.x bot.download() instead of document.download() ([205c8d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/205c8d987d93151a17aa0793cb51bd99917aea97))
* use AwareDateTime TypeDecorator for all datetime columns ([a7f3d65](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7f3d652c51ecd653900a530b7d38feaf603ecf1))
* use callback fallback when MINIAPP_CUSTOM_URL is not set ([eaf3a07](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eaf3a07579729031030308d77f61a5227b796c02))
* use direct is_trial access, add missing error codes to promo APIs ([69a9899](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69a9899d40dda83e83cbdba1aa43d9d1f756704b))
* use event field directly as event_name (already includes scope prefix) ([9aa22af](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9aa22af3390a249d1b500d75a7d7189daaed265e))
* use float instead of int | float (PYI041) ([310edae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/310edae013973d8533051088f3720cc5da3651b5))
* use flush instead of commit in server counter functions ([6cec024](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6cec024e46ef9177cb59aa81590953c9a75d81bb))
* use get_rendered_override for proper variable substitution in guest email overrides ([c165cca](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c165cca3239c9a1249aae9e5e712f7e34fb01107))
* use SAVEPOINT instead of full rollback in sync user creation ([2a90f87](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2a90f871b97b2b7ee8289e62294c65f8becb2539))
* use selection.period.days instead of selection.period_days ([4541016](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/45410168afe683675003a1c41c17074a54ce04f1))
* use short TTL fallback in restore_merge_token on parse error ([0e8c61a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0e8c61a7762ae796284144056c0cbdbcb53b6c7c))
* use sync context manager for structlog bound_contextvars ([25e8c9f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25e8c9f8fc4d2c66d5a1407d3de5c7402dc596da))
* use traffic topup config and add WATA 429 retry ([b5998ea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b5998ea9d22644ed2914b0e829b3a76a32a69ddf))
* validate payment sub-option suffix and harden payment method handling ([5f01783](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f01783dcb63f2f8bc20fef935d74d7588273aea))
* webhook notification 'My Subscription' button uses unregistered callback_data ([1e2a7e3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1e2a7e3096af11540184d60885b8c08d73506c4a))
* webhook:close button not working due to channel check timeout ([019fbc1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/019fbc12b6cf61d374bbed4bce3823afc60445c9))
* wrap user deletion steps in savepoints to prevent transaction cascade abort ([a38dfcb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a38dfcb75a47a185d979a8202f637d8b79812e67))
* безопасность и качество кода лендингов — 16 исправлений ([ef45095](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ef450955e6b37d437dabac55da037f53ca1f75dc))
* гарантировать положительный доход от подписок и исправить общий доход ([93a55df](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/93a55df4c0ac099946d440ec79fefb24327ab0e1))
* дедупликация promocode_uses при мерже аккаунтов ([00a7db2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/00a7db26905d53a9a978aaf6b97800ca3042b957))
* добавить create_transaction для 6 потоков оплаты с баланса ([374907b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/374907b6078c483531061465983e23f281e841a2))
* добавить create_transaction и admin-уведомления для автопродлений ([9f35088](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9f35088788c971cb757936dba7214abe54477af0))
* добавить ON DELETE CASCADE/SET NULL на все FK к users.id ([34c82c3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/34c82c348829cf528154bd1e2f5d77006d7ed5da))
* добавить пробелы в формат тарифов (1000 ГБ / 2 📱) ([900be65](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/900be65617dd5bbc6ffdcc82bb5504e1a93ead95))
* дубликаты системных ролей при переименовании и сброс permissions ([7a7fb71](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7a7fb71bf535e2a501f0677747ba63ca0b27ede5))
* изолировать stored_amount от downstream consumers в create_transaction ([b87535a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b87535ad4842cbf1f99f6fc1e28b5932fa5e3baa))
* исправления системы реферальных конкурсов ([6713b34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6713b3497854e73dddc212280d7bf12db818f38a))
* кнопка «Назад» в тарифах ведёт в админ панель, а не в настройки ([04562fd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/04562fd7e74de26776517549730819389b24a0d0))
* миграция 0016 падает если FK constraint отсутствует в БД ([15fe45d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/15fe45d11341001714599f8db963d182dc371aa3))
* миграция 0021 — drop server_default перед сменой типа на JSON ([3d3bb3b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3d3bb3badb55511960ed9b2a29ea67e0f0c3f26c))
* передать явный диапазон дат для all_time_stats в дашборде ([968d147](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/968d14704610eed528bca28cbf295c1ba1644a5a))
* показывать кнопку покупки тарифа вместо ошибки для триальных подписок ([acfa4b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/acfa4b3c2ea96e74d93470085265df76ec50e1e6))
* показывать только активные провайдеры на странице /profile/accounts ([9d7a557](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d7a557ef0e294ce9920e9953bb1358656ff9b81))
* промокоды — конвертация триалов, race condition, savepoints ([7fb839a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7fb839aef6234294b95064f9575c19d5a0c3f892))
* реактивация DISABLED подписок при покупке трафика для LIMITED пользователей ([7d28f55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7d28f5516a52606280219cbea846fba431da80d2))
* реактивация DISABLED подписок при покупке устройств и в REST API ([b9e17be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b9e17be8554a65eaf765a0b5b36fee062205c66f))
* синхронизация версии pyproject.toml с main и обновление uv в Dockerfile ([b31a893](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b31a893b13b2db911e51298ceb0107419f9a4cb3))
* убрать WITHDRAWAL из автонегации, добавить abs() в агрегации, исправить all_time_stats ([6da61d7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6da61d79510f7e05310f3cc020515b4dd0b3eb34))
* убрать избыточный минус в amount_kopeks для create_transaction ([849b3a7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/849b3a7034f2291db40e049c12e1b7c71b58bab1))
* устранение race condition при покупке устройств через re-lock после коммита ([a7a18dd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7a18dd0d1d59c64f7e4dd3ddc1b8cec47198077))
* устранение race conditions и атомарность платёжной системы ([4984f20](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4984f20e8fb030ee338723d797d51aee21f67ca8))
* устранение каскадного PendingRollbackError при восстановлении бэкапа ([8259278](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/82592784d0da8b8718f3b3aa34076af59ad2a878))


### Performance

* cache logo file_id to avoid re-uploading on every message ([142ff14](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/142ff14a502e629446be7d67fab880d12bee149d))


### Refactoring

* complete structlog migration with contextvars, kwargs, and logging hardening ([1f0fef1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1f0fef114bd979b2b0d2bd38dde6ce05e7bba07b))
* extract shared OAuth linking logic, add Literal types for providers ([f7caf0d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f7caf0de709ca6a46283f0b1928e34f8908f2c93))
* improve log formatting — logger name prefix and table alignment ([f637204](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f63720467a935bdaaa58bb34d588d65e46698f26))
* remove "both" mode from BOT_RUN_MODE, keep only polling and webhook ([efa3a5d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/efa3a5d4579f24dabeeba01a4f2e981144dd6022))
* remove Flask, use FastAPI exclusively for all webhooks ([119f463](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/119f463c36a95685c3bc6cdf704e746b0ba20d56))
* remove legacy app-config.json system ([295d2e8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/295d2e877e43f48e9319ba0b01be959904637000))
* remove modem functionality from classic subscriptions ([ee2e79d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ee2e79db3114fe7a9852d2cd33c4b4fbbde311ea))
* remove smart auto-activation & activation prompt, fix production bugs ([a3903a2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a3903a252efdd0db4b42ca3fd6771f1627050a7f))
* replace universal_migration.py with Alembic ([b6c7f91](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b6c7f91a7c79d108820c9f89c9070fde4843316c))
* replace universal_migration.py with Alembic ([784616b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/784616b349ef12b35ee021dd7a7b2a2ef9fc57f6))

## [3.23.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.23.1...v3.23.2) (2026-03-06)


### Bug Fixes

* device_limit fallback 1→0 для корректного отображения безлимита ([3e26832](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3e26832e745368a0dab2617e4e8ae2c410c6bca2))
* sync uv.lock version with pyproject.toml 3.23.1 ([8eb6a8c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8eb6a8c4606a0ea48e383c031ad83219fc8e062b))
* sync uv.lock version with pyproject.toml 3.23.1 ([bc52fd2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc52fd27113f95a4154b1990142d46ae606fd2e0))
* миграция 0016 падает если FK constraint отсутствует в БД ([15fe45d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/15fe45d11341001714599f8db963d182dc371aa3))

## [3.23.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.23.0...v3.23.1) (2026-03-06)


### Bug Fixes

* complete FK migration — add 27 missing constraints, fix broadcast_history nullable ([fe393d2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe393d2ca6ce302d8213cc751842ea92ef277e76))
* UniqueViolation при мерже аккаунтов с общим OAuth/telegram/email ID ([1c89bd8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1c89bd8b2acfe49de2c97dd75446a037a54fded7))
* дедупликация promocode_uses при мерже аккаунтов ([00a7db2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/00a7db26905d53a9a978aaf6b97800ca3042b957))
* добавить ON DELETE CASCADE/SET NULL на все FK к users.id ([34c82c3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/34c82c348829cf528154bd1e2f5d77006d7ed5da))
* дубликаты системных ролей при переименовании и сброс permissions ([7a7fb71](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7a7fb71bf535e2a501f0677747ba63ca0b27ede5))
* исправления системы реферальных конкурсов ([6713b34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6713b3497854e73dddc212280d7bf12db818f38a))
* кнопка «Назад» в тарифах ведёт в админ панель, а не в настройки ([04562fd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/04562fd7e74de26776517549730819389b24a0d0))
* промокоды — конвертация триалов, race condition, savepoints ([7fb839a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7fb839aef6234294b95064f9575c19d5a0c3f892))
* устранение race conditions и атомарность платёжной системы ([4984f20](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4984f20e8fb030ee338723d797d51aee21f67ca8))

## [3.23.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.22.0...v3.23.0) (2026-03-05)


### New Features

* account linking and merge system for cabinet ([dc7b8dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc7b8dc72a3a398d6270a0a2b8ce9e2b54cb9af7))
* account merge system — atomic user merge with full FK coverage ([2664b49](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2664b4956d8436a2720d7cd5992b8cdbb72cdbd9))
* add dedicated sales_stats RBAC permission section ([8f29e2e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8f29e2eee2e0c78f7f7e87a322eaf4bd4221069c))
* add server-complete OAuth linking endpoint for Mini App flow ([f867989](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f867989557d20378cfe815c9c88e1a842c4f6654))
* add Telegram account linking endpoint with security hardening ([da40d56](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/da40d5662d6d064090769823d616d6f9748ab5b9))


### Bug Fixes

* abs() for transaction amounts in admin notifications and subscription events ([fd139b2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fd139b28a2c45cc3fbd2e01707fb83fbabf57c71))
* add abs() to expenses query, display flip, contest stats, and recent payments ([de6f806](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/de6f80694ba8aa240764e2769ec04c16fe7f3672))
* add IntegrityError handling on link commit and format fixes ([0c1dc58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c1dc580c67254d11ffb096c22d8c8d78ac18e2b))
* add missing mark_as_paid_subscription, fix operation order, remove dead code ([5f2d855](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5f2d855702dea838b38887a5f44b9ad759acd5cf))
* auto-update permissions for system roles on bootstrap ([eff74be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eff74bed5bcc47a6cfa05c20cad14a40c1572d1f))
* centralize balance deduction and fix unchecked return values ([0466528](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0466528925a24087b8522a10cbb11c947c2b7d91))
* centralize has_had_paid_subscription into subtract_user_balance ([e4a6aad](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e4a6aad621be7ef4e7aedb21373927ede0c8d0a5))
* clean email verification and password fields from secondary user during merge ([7b4e948](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7b4e9488f6fbd1271f063579e48ca9a3c96cb645))
* consume promo offer in miniapp tariff-mode renewal path ([b8857e7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b8857e789ef60cf0c8766abbeadd094f62070a61))
* consume promo offer in tariff_purchase.py, fix negative transaction amount ([c8ef808](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c8ef80853915af3e3eb254edd07d8d78b66a9282))
* delete cross-referral earnings before bulk reassignment, clear secondary.referred_by_id ([f204b67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f204b678803297ce60faad628d16f46344b11ed0))
* from redis.exceptions import NoScriptError ([667291a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/667291a2dcaeae21e27eeb6376085e69caa4e45a))
* harden account merge security and correctness ([d855e9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d855e9e47fab1a038e581437a9921bdfeb11e927))
* **merge:** validate before consuming token, add flush, defensive balance ([bc1e6fb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc1e6fb22c6e23c7a34364796f51a55c60224aff))
* negative balance transfer, linking state validation, referrer migration ([531d5cf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/531d5cff3019e72dde6ee64977cb801e8f8c8d0b))
* prevent concurrent device purchases exceeding max device limit ([1cfede2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1cfede28b7570bcaf77cb53d6b2a9f3b0e4e9408))
* prevent infinite reuse of first_purchase_only promo code discounts ([2cec8dc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cec8dc4a487017f4b1c5ca80710f2d70045b825))
* prevent self-referral loops, invalidate all sessions on merge ([db61365](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/db61365e11ccec4dd45671b33da00f4b05484589))
* reassign orphaned records on merge, eliminate TOCTOU race ([d7a9d2b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7a9d2bfba5b796882d3e04be6038b766cd0a4c8))
* redis cache uses sync client due to import shadowing ([667291a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/667291a2dcaeae21e27eeb6376085e69caa4e45a))
* restore merge token on DB failure, fix partner_status priority ([9582758](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9582758d1c85735c8ead8cbfeb56bbdae45288af))
* review findings — exception chaining, redundant unquote, validator tightening ([467dea1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/467dea1315fbaf8d09ccbba292cd0bcc60d9f3ab))
* second round review fixes for account merge ([64ee045](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/64ee0459e4e3d3fe87ad65387fcbcb147147ac1b))
* use short TTL fallback in restore_merge_token on parse error ([0e8c61a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0e8c61a7762ae796284144056c0cbdbcb53b6c7c))
* гарантировать положительный доход от подписок и исправить общий доход ([93a55df](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/93a55df4c0ac099946d440ec79fefb24327ab0e1))
* добавить create_transaction для 6 потоков оплаты с баланса ([374907b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/374907b6078c483531061465983e23f281e841a2))
* добавить create_transaction и admin-уведомления для автопродлений ([9f35088](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9f35088788c971cb757936dba7214abe54477af0))
* добавить пробелы в формат тарифов (1000 ГБ / 2 📱) ([900be65](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/900be65617dd5bbc6ffdcc82bb5504e1a93ead95))
* изолировать stored_amount от downstream consumers в create_transaction ([b87535a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b87535ad4842cbf1f99f6fc1e28b5932fa5e3baa))
* передать явный диапазон дат для all_time_stats в дашборде ([968d147](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/968d14704610eed528bca28cbf295c1ba1644a5a))
* показывать кнопку покупки тарифа вместо ошибки для триальных подписок ([acfa4b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/acfa4b3c2ea96e74d93470085265df76ec50e1e6))
* показывать только активные провайдеры на странице /profile/accounts ([9d7a557](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d7a557ef0e294ce9920e9953bb1358656ff9b81))
* реактивация DISABLED подписок при покупке трафика для LIMITED пользователей ([7d28f55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7d28f5516a52606280219cbea846fba431da80d2))
* реактивация DISABLED подписок при покупке устройств и в REST API ([b9e17be](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b9e17be8554a65eaf765a0b5b36fee062205c66f))
* синхронизация версии pyproject.toml с main и обновление uv в Dockerfile ([b31a893](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b31a893b13b2db911e51298ceb0107419f9a4cb3))
* убрать WITHDRAWAL из автонегации, добавить abs() в агрегации, исправить all_time_stats ([6da61d7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6da61d79510f7e05310f3cc020515b4dd0b3eb34))
* убрать избыточный минус в amount_kopeks для create_transaction ([849b3a7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/849b3a7034f2291db40e049c12e1b7c71b58bab1))
* устранение race condition при покупке устройств через re-lock после коммита ([a7a18dd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7a18dd0d1d59c64f7e4dd3ddc1b8cec47198077))
* устранение каскадного PendingRollbackError при восстановлении бэкапа ([8259278](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/82592784d0da8b8718f3b3aa34076af59ad2a878))


### Refactoring

* extract shared OAuth linking logic, add Literal types for providers ([f7caf0d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f7caf0de709ca6a46283f0b1928e34f8908f2c93))

## [3.22.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.21.0...v3.22.0) (2026-03-04)


### New Features

* replace pip with uv in Dockerfile ([e23d69f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e23d69fcec7ab65a14b054fd46f6ecf87ae6fd13))


### Bug Fixes

* add selectinload for campaign registrations in list query ([4d74afd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4d74afd7118524623371f904a93ae1fcbba8d64e))
* backup restore fails on FK constraints and transaction poisoning ([ff1c872](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ff1c8722c9188fdbaf765d6b7e9192686df64850))
* classic mode prices overridden by active tariff prices ([628a99e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/628a99e7aa0812842dabc430857190c0cd5c2680))
* close remaining daily subscription expire paths ([618c936](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/618c936ac9ce4904cd784bf2278d3da188895f2d))
* empty JSONB values exported as None in backup ([57aaca8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57aaca82f5bf9d7bdd9d4b924aa3412d85eccbb5))
* handle duplicate remnawave_uuid on email sync ([eaeee7a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eaeee7a765c03ff33e2928cdb41be91948eca95c))
* MissingGreenlet on campaign registrations access ([018f18f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/018f18fa0c9bba1a1dbca8b2398b9611d0c94c36))
* prevent daily subscriptions from being expired by middleware/CRUD/webhook ([0ed6397](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0ed6397fa9e5810fcffc9152ab2241fcf37cf85a))
* reset traffic purchases on expired subscription renewal + pricing fixes ([dce9eaa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dce9eaa5971cb1dc0945747e02397a250e8e411b))

## [3.21.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.20.1...v3.21.0) (2026-03-02)


### New Features

* add admin campaign chart data endpoint with deposits/spending split ([fa7de58](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fa7de589c1bd0ae37ebaaa07bae0ed3d68e01720))
* add admin sales statistics API with 6 analytics endpoints ([58faf9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/58faf9eaeca63c458093d2a5e74a860f57712ab0))
* add daily deposits by payment method breakdown ([d33c5d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d33c5d6c07ce4a9efaf3c5aceb448e968e1b8ed7))
* add daily device purchases chart to addons stats ([2449a5c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2449a5cbbe5179a762197414a5752896383a6ee4))
* add desired commission percent to partner application ([7ea8fbd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7ea8fbd584aff2127595001094ef69acb52f847f))
* add RESET_TRAFFIC_ON_TARIFF_SWITCH admin setting ([4eaedd3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4eaedd33bf697469fe9ed6a1bfe8b59ca43b46fb))
* enhance sales stats with device purchases, per-tariff daily breakdown, and registration tracking ([31c7e2e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/31c7e2e9c14cb88762a62a72e4f65051e0c6c1fd))


### Bug Fixes

* add exc_info traceback to sync user error log ([efdf2a3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/efdf2a3189a2f790e570f9a6e19d91469be4ea4f))
* add local traffic_used_gb reset in all tariff switch handlers ([2cdbbc0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cdbbc09ba9a19dcb720049ffde08ba780ac5751))
* add min_length to state field, use exc_info for referral warning ([062c486](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/062c4865db194f9d2242772044402fa2711a69bd))
* add missing subscription columns migration ([b96e819](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b96e819da4cc37710e9fc17467045b33bcffac4d))
* address review findings from agent verification ([cc5be70](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cc5be7059fdf4cefb01e97196c825b217f8b54b3))
* correct cart notification after balance top-up ([2fab50c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2fab50c340c885fc92a4bf797a4b03da6e44af31))
* correct referral withdrawal balance formula and commission transaction type ([83c6db4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/83c6db48349440447305604e944fa440bdceb3fb))
* count sales from completed payment transactions instead of subscription created_at ([06c3996](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/06c3996da4fa14eafb294651158068c7cda51e52))
* eliminate double panel API call on tariff change, harden cart notification ([b2cf4aa](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b2cf4aaa91f3fb63dca7e70645cadb75aa158cfe))
* eliminate referral system inconsistencies ([60c97f7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/60c97f778bc4cc18aaf4d8a31826bc831c3b3f8f))
* email verification bypass, ban-notifications size limit, referral balance API ([256cbfc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/256cbfcadfd2fc88d8de69557c78618639af157d))
* enforce user restrictions in cabinet API and fix poll history crash ([faba3a8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/faba3a8ed6d428305f9ca7d7fd9bdcc1fd72ba52))
* freekassa OP-SP-7 error and missing telegram notification ([200f91e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/200f91ef1748bb6213d1ef3a8e83ae976290a8a7))
* generate missing crypto link on the fly and skip unresolved templates ([4c72058](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4c72058d4ad8b0594991b17323928d9004803bfa))
* handle expired callback queries and harden middleware error handling ([f52e6ae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f52e6aedac3de1c9bb2ad1a5a16b06d38b79ab63))
* handle expired ORM attributes in sync UUID mutation ([9ae5d7b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ae5d7bb60c57e2c29d6f3c5098c23450d5feb61))
* handle NULL used_promocodes for migrated users ([cdcabee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cdcabee80d1d7f0b367a97cdec20bb49e8592115))
* hide traffic topup button when tariff doesn't support it ([399ca86](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/399ca86561f4271e9c542bac87c0dd2931a223e0))
* improve campaign routes, schemas, and add database indexes ([ded5c89](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ded5c899f7425707b17fef4d0d5ceafac777ef08))
* include desired_commission_percent in admin notification ([dc3d22f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc3d22f52db40150d595bccf524d38790e5725d9))
* migrate VK OAuth to VK ID OAuth 2.1 with PKCE ([1dfa780](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1dfa78013c4fb926a2b32bf4d63baa28215e7340))
* partner system — CRUD nullable fields, per-campaign stats, atomic unassign, diagnostic logging ([ed3ae14](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ed3ae14d0c378fa0dc2d442c3aa5a70172f3132c))
* prevent squad drop on admin subscription type change, require subscription for wheel spins ([59f0e42](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/59f0e42be7e3c679d15cf2fc6820ab7097cd2201))
* prevent sync from overwriting subscription URLs with empty strings ([9c00479](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9c004791f28fbcf314b93c1b2a38593069605239))
* reject promo codes for days when user has no subscription or trial ([e32e2f7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e32e2f779d014d587b58d63b513fd913ae1b7a41))
* remove premature tariff_id assignment in _apply_extension_updates ([b47678c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b47678cfb0ba5897b37dfe1f94e3d1336af5698e))
* renewals stats empty on all-time filter ([e25fcfc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e25fcfc6ef941465b83f368f152304ea5a6747d9))
* resolve GROUP BY mismatch for daily_by_tariff query ([e5f29eb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e5f29eb041e88bc6315f0b4da3b78898d9dd7fff))
* restore panel user discovery on admin tariff change, localize cart reminder ([1256ddc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1256ddcd1a772f90e7bdf9437043a47ea9d84d53))
* separate base and purchased traffic in renewal pricing ([739ba29](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/739ba2986f41b04058eb14e8b87b0699fe96f922))
* sync traffic reset across all tariff switch code paths ([d708365](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d708365aca9dfd5c3afda1a1de4303e0bd1d263e))
* use .is_(True) and add or 0 guards per code review ([69b5ca0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69b5ca06701e7381c39448e2bf6b927f0558058c))
* use direct is_trial access, add missing error codes to promo APIs ([69a9899](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/69a9899d40dda83e83cbdba1aa43d9d1f756704b))
* use float instead of int | float (PYI041) ([310edae](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/310edae013973d8533051088f3720cc5da3651b5))
* use SAVEPOINT instead of full rollback in sync user creation ([2a90f87](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2a90f871b97b2b7ee8289e62294c65f8becb2539))

## [3.20.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.20.0...v3.20.1) (2026-02-25)


### Bug Fixes

* make migrations 0010/0011 idempotent, escape HTML in crash notification ([a696896](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a696896d2c4a3d0d6026398fcdc76ded9575375d))
* prevent race condition expiring active daily subscriptions ([bfef7cc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bfef7cc6296e296f17068e519469c3deaddc1b3b))

## [3.20.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.19.0...v3.20.0) (2026-02-25)


### New Features

* add separate Freekassa SBP and card payment methods ([0da0c55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0da0c5547d0648a70f848fe77c13d583f4868a52))
* add validation to animation config API ([a15403b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a15403b8b6e1ec1bb5c37fdde646e7790373e860))


### Bug Fixes

* initialize logger in bot_configuration.py ([988d0e5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/988d0e5c2f27538135d757187a0b6770f078b1d9))
* remove gemini-effect and noise from allowed background types ([731eb24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/731eb2436428d0e12f1e5ccdebc72cd74fd7c65e))
* resolve ruff lint errors (import sorting, unused variable) ([b2d7abf](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b2d7abf5bd10a98fd7ad1da50b5072afc65a5b48))
* resolve sync 404 errors, user deletion FK constraint, and device limit not sent to RemnaWave ([1ce9174](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1ce91749aa12ffcefcf66bea714cea218739f3fe))

## [3.19.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.18.0...v3.19.0) (2026-02-25)


### New Features

* add granular user permissions (balance, subscription, promo_group, referral, send_offer) ([60c4fe2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/60c4fe2e239d8fef7726cac769711c8fcce789eb))
* add per-channel disable settings and fix CHANNEL_REQUIRED_FOR_ALL bug ([3642462](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3642462670c876052aa668c1515af8c04234cb34))
* add RBAC + ABAC permission system for admin cabinet ([3fee54f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3fee54f657dc6e0db1ec36697850ada2235e6968))
* add resource_type and request body to audit log entries ([388fc7e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/388fc7ee67f5fc0edf6b7b64b977e12a2d8f0566))
* allow editing system roles ([f6b6e22](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f6b6e22a9528dc05b7fbfa80b63051a75c8e73cd))
* capture query params in audit log details for all requests ([bea9da9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bea9da96d44965fcee5e2eba448960443152d4ea))


### Bug Fixes

* address RBAC review findings (CRITICAL + HIGH) ([1646f04](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1646f04bde47a08f3fd782b7831d40760bd1ba60))
* align RBAC route prefixes with frontend API paths ([5a7dd3f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5a7dd3f16408f3497a9765e79a540ccdabc50e69))
* always include details in successful audit log entries ([3dc0b93](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3dc0b93bdfc85fb97f371dc34e024272766afc65))
* extract real client IP from X-Forwarded-For/X-Real-IP headers ([af6686c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/af6686ccfae12876e867cdabe729d0c893bd85a1))
* grant legacy config-based admins full RBAC access ([8893fc1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8893fc128e3d8927054f1df1647e896e780c69e7))
* improve campaign notifications and ticket media in admin topics ([a594a0f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a594a0f79f48227f75d6102b4586179102c4d344))
* RBAC API response format fixes and audit log user info ([4598c27](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4598c2785a42773ee8be04ada1c00d14824e07e0))
* RBAC audit log action filter and legacy admin level ([c1da8a4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c1da8a4dba5d0c993d3e15b2866bdcfa09de1752))
* restore subscription_url and crypto_link after panel sync ([26efb15](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/26efb157e476a18b036d09167628a295d7e4c10b))
* specify foreign_keys on User.admin_roles_rel to resolve ambiguous join ([bc7d061](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bc7d0612f1476f2fdb498cd76a9374b41fd9440a))
* stack promo group + promo offer discounts in bot (matching cabinet) ([628997f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/628997fb48413cc4fae9ac491d1c7f6185877200))

## [3.18.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.17.1...v3.18.0) (2026-02-24)


### New Features

* add ChatTypeFilterMiddleware to ignore group/forum messages ([25f014f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25f014fd8988b5513fba8fec4483981384687e96))
* add multi-channel mandatory subscription system ([8375d7e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8375d7ecc5e54ea935a00175dd26f667eab95346))
* add required channels button to admin settings submenu in bot ([3af07ff](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3af07ff627fc354da4f8c41b0bd0575dddd9afa5))
* colored channel subscription buttons via Bot API 9.4 style ([0b3b2e5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0b3b2e5dc54d8b6b3ede883d5c0f5b91791b7b9b))
* rework guide mode with Remnawave API integration ([5a269b2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5a269b249e8e6cad266822095676937481613f5f))


### Bug Fixes

* add missing CHANNEL_CHECK_NOT_SUBSCRIBED localization key ([a47ef67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a47ef67090c4e48f466286f7c676eeee0c61a4fb))
* address code review issues in guide mode rework ([fae6f71](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fae6f71def421e319733e4edcf1ca80a2831b2ec))
* address security review findings ([6feec1e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6feec1eaa847644ba3402763a2ffefd8f770cc01))
* callback routing safety and cache invalidation order ([6a50013](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6a50013c21de199df0ba0dab3600b693548b6c1e))
* correct broadcast button deep-links for cabinet mode ([e5fa45f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e5fa45f74f969b84f9f1388f8d4888d22c46d7e8))
* HTML-escape all externally-sourced text in guide messages ([711ec34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/711ec344c646844401f355695a7e8c0d4fb401ee))
* improve deduplication log message wording in monitoring service ([2aead9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2aead9a68b6bf274c8d1497c85f2ed4d4fc9c70b))
* invalidate app config cache on local file saves ([978726a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/978726a7856cf56257c49491afe569fa8c395eac))
* pre-existing bugs found during review ([1bb939f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1bb939f63a360a687fafba26bc363024df0f6be0))
* remove [@username](https://github.com/username) channel ID input, auto-prefix -100 for bare digits ([a7db469](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7db469fd7603e7d8dac3076f5d633da654a3a57))
* restore RemnaWave config management endpoints ([6f473de](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6f473defef32a6d81cee55ef2cd397d536a784a7))
* translate required channels handler to Russian, add localization keys ([1bc9074](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1bc9074c1bcdaba7215065c77aac9dd51db4d7c8))


### Refactoring

* remove legacy app-config.json system ([295d2e8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/295d2e877e43f48e9319ba0b01be959904637000))

## [3.17.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.17.0...v3.17.1) (2026-02-23)


### Bug Fixes

* add diagnostic logging for device_limit sync to RemnaWave ([97b3f89](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97b3f899d12c4bf32b6229a3b595f1b9ad611096))
* add int32 overflow guards and strengthen auth validation ([50a931e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/50a931ec363d1842126b90098f93c6cae47a9fac))
* add missing broadcast_history columns and harden subscription logic ([d4c4a8a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d4c4a8a211eaf836024f8d9dcb725f25f514f05e))
* allow tariff switch when less than 1 day remains ([67f3547](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/67f3547ae2f40153229d71c1abe7e1213466e5c3))
* cap expected_monthly_referrals to prevent int32 overflow ([2ef6185](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2ef618571570edb6011a365af8aa9cd7e3348c2e))
* cross-validate Telegram identity on every authenticated request ([973b3d3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/973b3d3d3ff80376c0fd19c531d7aac3ae751df8))
* handle RemnaWave API errors in traffic aggregation ([ed4624c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ed4624c6649bdbc04bc850ef63e5c86e26a37ce4))
* migrate all remaining naive timestamp columns to timestamptz ([708bb9e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/708bb9eec7ea4360b26709fb2a3f82dd139ed600))
* prevent partner self-referral via own campaign link ([115c0c8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/115c0c84c0698591da75d7d3b8fbd8e0fc8541ea))
* protect active paid subscriptions from being disabled in RemnaWave ([1b6bbc7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b6bbc7131341b4afd739e4195f02aa956ead616))
* repair missing DB columns and make backup resilient to schema mismatches ([c20355b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c20355b06df13328f85cc5a6045b3e490419a30a))
* show negative amounts for withdrawals in admin transaction list ([5ee45f9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5ee45f97d179ce2d32b3f19eeb6fd01989a30ca7))
* suppress web page preview when logo mode is disabled ([1f4430f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1f4430f3af8f3efcc58ef7b562904adcb1640a44))
* uploaded backup restore button not triggering handler ([ebe5083](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ebe508302b906f8b56cb230b934fb8566990c684))
* use aiogram 3.x bot.download() instead of document.download() ([205c8d9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/205c8d987d93151a17aa0793cb51bd99917aea97))

## [3.17.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.16.3...v3.17.0) (2026-02-18)


### New Features

* add referral code tracking to all cabinet auth methods + email_templates migration ([18c2477](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/18c24771737994f3ae1f832435ed2247ca625aab))


### Bug Fixes

* prevent 'caption is too long' error in logo mode ([6e28a1a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6e28a1a22b02055b357051dfecbee7fefbebc774))
* skip blocked users in trial notifications and broadcasts without DB status change ([493f315](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/493f315a65610826a04e04c3d2065e0b395426ed))

## [3.16.3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.16.2...v3.16.3) (2026-02-18)


### Bug Fixes

* 3 user deletion bugs — type cast, inner savepoint, lazy load ([af31c55](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/af31c551d2f23ef01425bdb2db8f255dbc3047e2))
* auth middleware catches all commit errors, not just connection errors ([6409b0c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6409b0c023cd7957c43d5c1c3d83e671ccaf959c))
* connected_squads stores UUIDs, not int IDs — use get_server_ids_by_uuids ([d7039d7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d7039d75a47fbf67436a9d39f2cd9f65f2646544))
* deadlock on user deletion + robust migration 0002 ([b7b83ab](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b7b83abb723913b3167e7462ff592a374c3f421b))
* eliminate deadlock by matching lock order with webhook ([d651a6c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d651a6c02f501b7a0ded570f2db6addcc16173a9))
* make migration 0002 robust with table existence checks ([f076269](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f076269c323726c683a38db092d907591a26e647))
* wrap user deletion steps in savepoints to prevent transaction cascade abort ([a38dfcb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a38dfcb75a47a185d979a8202f637d8b79812e67))

## [3.16.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.16.1...v3.16.2) (2026-02-18)


### Bug Fixes

* auto-convert naive datetimes to UTC-aware on model load ([f7d33a7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f7d33a7d2b31145a839ee54676816aa657ac90da))
* extend naive datetime guard to all model properties ([bd11801](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bd11801467e917d76005d1a782c71f5ae4ffee6e))
* handle naive datetime in raw SQL row comparison (payment/common) ([38f3a9a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/38f3a9a16a24e85adf473f2150aad31574a87060))
* handle naive datetimes in Subscription properties ([e512e5f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e512e5fe6e9009992b5bc8b9be7f53e0612f234a))
* use AwareDateTime TypeDecorator for all datetime columns ([a7f3d65](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a7f3d652c51ecd653900a530b7d38feaf603ecf1))

## [3.16.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.16.0...v3.16.1) (2026-02-18)


### Bug Fixes

* add migration for partner system tables and columns ([4645be5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4645be53cbb3799aa6b2b6a623af30460357a554))
* add migration for partner system tables and columns ([79ea398](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79ea398d1db436a7812a799bf01b2c1c3b1b73be))

## [3.16.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.15.1...v3.16.0) (2026-02-18)


### New Features

* add admin notifications for partner applications and withdrawals ([cf7cc5a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cf7cc5a84e295608009f255fcd0dcedb5a2a04a3))
* add admin partner settings API (withdrawal toggle, requisites text, partner visibility) ([6881d97](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6881d97bbb1f6cd8ca3609c2d9286a6e4fb24fc3))
* add campaign_id to ReferralEarning for campaign attribution ([0c07812](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0c07812ecc9502f54a7745a77b086fc52bdc0e34))
* add partner system and withdrawal management to cabinet ([58bfaea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/58bfaeaddbcbb98cb67dbd507847a0e5c8d07809))
* attribute campaign registrations to partner for referral earnings ([767e965](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/767e9650285adc72b067b2c0b8a4d1ac5c5bba57))
* blocked user detection during broadcasts, filter blocked from all notifications ([10e231e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/10e231e52e0dbabd9195a2df373b3c95129a5e4f))
* enforce 1-to-1 partner-campaign binding with partner info in campaigns ([366df18](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/366df18c547047a7c69192c768970ebc6ee426fc))
* expose traffic_reset_mode in subscription response ([59383bd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/59383bdbd8c72428d151cb24d132452414b14fa3))
* expose traffic_reset_mode in tariff API response ([5d4a94b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5d4a94b8cea8f16f0b4c31e24a4695bee4c67af7))
* include partner campaigns in /partner/status response ([ea5d932](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ea5d932476553ad1750da3bebbd4b8f055478040))
* link campaign registrations to partner for referral earnings ([c4dc43e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c4dc43e054e9faec2f9614fe51a64635f80c1796))
* notify users on partner/withdrawal approve/reject ([327d4f4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/327d4f4d1559e37dc591adbfd0c839d986d1068d))


### Bug Fixes

* add blocked_count column migration to universal_migration.py ([b4b10c9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b4b10c998cadbb879540e56dbd0e362b5497ee57))
* add missing payment providers to payment_utils and fix {total_amount} formatting ([bdb6161](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bdb61613de378efab4de6de98fde2de3b554c548))
* add selectinload for subscription in campaign user list ([eb9dba3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eb9dba3f4728b478f2206ff992700a9677f879c7))
* campaign web link uses ?campaign= param, not ?start= ([28f524b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/28f524b7622ed975d2fece66edc94d9713354738))
* correct subscription_service import in broadcast cleanup ([6c4e035](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6c4e035146934dffb576477cc75f7365b2f27b99))
* critical security and data integrity fixes for partner system ([8899749](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/88997492c3534ea2f6e194c0382c77302557c2f3))
* handle YooKassa NotFoundError gracefully in get_payment_info ([df5b1a0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/df5b1a072d99ff8aee0c94304b2a0214f0fcffe7))
* medium-priority fixes for partner system ([7c20fde](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7c20fde4e887749d72280a8804467645e5bab416))
* move PartnerStatus enum before User class to fix NameError ([acc1323](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/acc1323a542b8e92433cabf1334d2d98bfa21e21))
* prevent fileConfig from destroying structlog handlers ([e78b104](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e78b1040a50ac14759bceab396d0c3e34dd79cdd))
* reorder button_click_logs migration to nullify before ALTER TYPE ([df5415f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/df5415f30b2aae4412ff5fbd3cac8076128b818c))
* resolve HIGH-priority performance and security issues in partner system ([fcf3a2c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fcf3a2c8062752b2b1dc06b5993ac2d8ae80ee85))
* return zeroed stats dict when withdrawal is disabled ([7883efc](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7883efc3d6e6d8bedf8e4b7d72634cbab6e2f3d7))
* unassign all campaigns when revoking partner status ([d39063b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d39063b22ffb6442e275db39704361cdb9251793))


### Refactoring

* replace universal_migration.py with Alembic ([b6c7f91](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b6c7f91a7c79d108820c9f89c9070fde4843316c))
* replace universal_migration.py with Alembic ([784616b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/784616b349ef12b35ee021dd7a7b2a2ef9fc57f6))

## [3.15.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.15.0...v3.15.1) (2026-02-17)


### Bug Fixes

* add naive datetime guards to fromisoformat() in Redis cache readers ([1b3e6f2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b3e6f2f11c20aa240da1beb11dd7dfb20dbe6e8))
* add naive datetime guards to fromisoformat() in Redis cache readers ([6fa4948](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6fa49485d9f1cd678cb5f9fa7d0375fd47643239))

## [3.15.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.14.1...v3.15.0) (2026-02-17)


### New Features

* add LOG_COLORS env setting to toggle console ANSI colors ([27309f5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/27309f53d9fa0ba9a2ca07a65feed96bf38f470c))
* add web campaign links with bonus processing in auth flow ([d955279](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9552799c17a76e2cc2118699528c5b591bd97fb))


### Bug Fixes

* AttributeError in withdrawal admin notification (send_to_admins → send_admin_notification) ([c75ec0b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c75ec0b22a3f674d3e1a24b9d546eca1998701b3))
* remove local UTC re-imports shadowing module-level import in purchase.py ([e68760c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e68760cc668016209f4f19a2e08af8680343d6ed))

## [3.14.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.14.0...v3.14.1) (2026-02-17)


### Bug Fixes

* add naive datetime guards to parsers and fix test datetime literals ([0946090](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/094609005af7358bf5d34d252fc66685bd25751c))
* address remaining abs() issues from review ([ff21b27](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ff21b27b98bb5a7517e06057eb319c9f3ebb74c7))
* complete datetime.utcnow() → datetime.now(UTC) migration ([eb18994](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eb18994b7d34d777ca39d3278d509e41359e2a85))
* normalize transaction amount signs across all aggregations ([4247981](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4247981c98111af388c98628c1e61f0517c57417))
* prevent negative amounts in spent display and balance history ([c30972f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c30972f6a7911a89a6c3f2080019ff465d11b597))

## [3.14.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.13.0...v3.14.0) (2026-02-16)


### New Features

* show all active webhook endpoints in startup log ([9d71005](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9d710050ad40ba76a14aa6ace8e8a47f25cdde94))


### Bug Fixes

* force basicConfig to replace pre-existing handlers ([7eb8d4e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7eb8d4e153bab640a5829f75bfa6f70df5763284))
* NameError in set_user_devices_button — undefined action_text ([1b8ef69](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1b8ef69a1bbb7d8d86827cf7aaa4f05cbf480d75))
* remove unused PaymentService from MonitoringService init ([491a7e1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/491a7e1c425a355e55b3020e2bcc7b96047bdf5e))
* resolve MissingGreenlet error when accessing subscription.tariff ([a93a32f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a93a32f3a7d1b259a2e24954ae5d2b7c966c5639))
* sync support mode from cabinet admin to SupportSettingsService ([516be6e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/516be6e600a08ad700d83b793dc64b2ca07bdf44))
* sync SUPPORT_SYSTEM_MODE between SystemSettings and SupportSettings ([0807a9f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0807a9ff19d1eb4f1204f7cbeb1da1c1cfefe83a))


### Refactoring

* improve log formatting — logger name prefix and table alignment ([f637204](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f63720467a935bdaaa58bb34d588d65e46698f26))

## [3.13.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.12.1...v3.13.0) (2026-02-16)


### New Features

* colored console logs via structlog + rich + FORCE_COLOR ([bf64611](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bf646112df02aa7aa7918d0513cb6968ceb7f378))


### Bug Fixes

* limit Rich traceback output to prevent console flood ([11ef714](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11ef714e0dde25a08711c0daeee943b6e71e20b7))
* resolve exc_info for admin notifications, clean log formatting ([11f8af0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/11f8af003fc60384abafa2b670b89d6ad3ac57a4))
* suppress startup log noise (~350 lines → ~30) ([8a6650e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8a6650e57cd8ea396d9b057a7753469947f38d29))
* traceback in Telegram notifications + reduce log padding ([909a403](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/909a4039c43b910761bd05c36e79c8e6773199db))
* use sync context manager for structlog bound_contextvars ([25e8c9f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/25e8c9f8fc4d2c66d5a1407d3de5c7402dc596da))


### Refactoring

* complete structlog migration with contextvars, kwargs, and logging hardening ([1f0fef1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1f0fef114bd979b2b0d2bd38dde6ce05e7bba07b))

## [3.12.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.12.0...v3.12.1) (2026-02-16)


### Bug Fixes

* add /start burst rate-limit to prevent spam abuse ([61a9722](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/61a97220d30031816ab23e33a46717e4895c0758))
* add promo code anti-abuse protections ([97ec39a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/97ec39aa803f0e3f03fdcd482df0cbcb86fd1efd))
* handle TelegramBadRequest in ticket edit_message_text calls ([8e61fe4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8e61fe47746da2ac09c3ea8c4dbfc6be198e49e3))
* replace deprecated Query(regex=) with pattern= ([871ceb8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/871ceb866ccf1f3a770c7ef33406e1a43d0a7ff7))

## [3.12.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.11.0...v3.12.0) (2026-02-15)


### New Features

* add 'default' (no color) option for button styles ([10538e7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/10538e735149bf3f3f2029ff44b94d11d48c478e))
* add button style and emoji support for cabinet mode (Bot API 9.4) ([bf2b2f1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bf2b2f1c5650e527fcac0fb3e72b4e6e19bef406))
* add per-button enable/disable toggle and custom labels per locale ([68773b7](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/68773b7e77aa344d18b0f304fa561c91d7631c05))
* add per-section button style and emoji customization via admin API ([a968791](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a9687912dfe756e7d772d96cc253f78f2e97185c))
* add web admin button for admins in cabinet mode ([9ac6da4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9ac6da490dffa03ce823009c6b4e5014b7d2bdfb))
* rename MAIN_MENU_MODE=text to cabinet with deep-linking to frontend sections ([ad87c5f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ad87c5fb5e1a4dd0ef7691f12764d3df1530f643))


### Bug Fixes

* daily tariff subscriptions stuck in expired/disabled with no resume path ([80914c1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/80914c1af739aa0ee1ea75b0e5871bf391b9020d))
* filter out traffic packages with zero price from purchase options ([64a684c](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/64a684cd2ff51e663a1f70e61c07ca6b4f6bfc91))
* handle photo message in ticket creation flow ([e182280](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e1822800aba3ea5eee721846b1e0d8df0a9398d1))
* handle tariff_extend callback without period (back button crash) ([ba0a5e9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ba0a5e9abd9bd582968d69a5c6e57f336094c782))
* pre-validate CABINET_BUTTON_STYLE to prevent invalid values from suppressing per-section defaults ([46c1a69](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/46c1a69456036cb1be784b8d952f27110e9124eb))
* remove redundant trial inactivity monitoring checks ([d712ab8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d712ab830166cab61ce38dd32498a8a9e3e602b0))
* webhook notification 'My Subscription' button uses unregistered callback_data ([1e2a7e3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1e2a7e3096af11540184d60885b8c08d73506c4a))

## [3.11.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.10.3...v3.11.0) (2026-02-12)


### New Features

* add cabinet admin API for pinned messages management ([1a476c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1a476c49c19d1ec2ab2cda1c2ffb5fd242288bb6))
* add startup warnings for missing HAPP_CRYPTOLINK_REDIRECT_TEMPLATE and MINIAPP_CUSTOM_URL ([476b89f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/476b89fe8e613c505acfc58a9554d31ccf92718a))


### Bug Fixes

* add passive_deletes to Subscription relationships to prevent NOT NULL violation on cascade delete ([bfd66c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bfd66c42c1fba3763f41d641cea1bd101ec8c10c))
* add startup warning for missing HAPP_CRYPTOLINK_REDIRECT_TEMPLATE in guide mode ([1d43ae5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1d43ae5e25ffcf0e4fe6fec13319d393717e1e50))
* flood control handling in pinned messages and XSS hardening in HTML sanitizer ([454b831](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/454b83138e4db8dc4f07171ee6fe262d2cd6d311))
* suppress expired callback query error in AuthMiddleware ([2de4384](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2de438426a647e2bcae9b4d99eef4093ff8b5429))
* ticket creation crash and webhook PendingRollbackError ([760c833](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/760c833b7402541d3c7cf2ed7fc0418119e75042))

## [3.10.3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.10.2...v3.10.3) (2026-02-12)


### Bug Fixes

* handle unique constraint conflicts during backup restore without clear_existing ([5893874](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/589387477624691e0026086800428e7e52e06128))
* harden backup create/restore against serialization and constraint errors ([fc42916](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fc42916b10bb698895eb75c0e2568747647555d3))
* resolve deadlock on server_squads counter updates and add webhook notification toggles ([57dc1ff](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/57dc1ff47f2f6183351db7594544a07ca6f27250))

## [3.10.2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.10.1...v3.10.2) (2026-02-12)


### Bug Fixes

* allow email change for unverified emails ([93bb8e0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/93bb8e0eb492ca59e29da86594e84e9c486fea65))
* clean stale squad UUIDs from tariffs during server sync ([fcaa9df](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fcaa9dfb27350ceda3765c6980ad67f671477caf))
* delete subscription_servers before subscription to prevent FK violation ([7d9ced8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7d9ced8f4f71b43ed4ac798e6ff904a086e1ac4a))
* handle StaleDataError in webhook user.deleted server counter decrement ([c30c2fe](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c30c2feee1db03f0a359b291117da88002dd0fe0))
* handle time/date types in backup JSON serialization ([27365b3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/27365b3c7518c09229afcd928f505d0f3f66213f))
* HTML parse fallback, email change race condition, username length limit ([d05ff67](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d05ff678abfacaa7e55ad3e55f226d706d32a7b7))
* payment race conditions, balance atomicity, renewal rollback safety ([c5124b9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/c5124b97b63eda59b52d2cbf9e2dcdaa6141ed6e))
* remove DisplayNameRestrictionMiddleware ([640da34](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/640da3473662cfdcceaa4346729467600ac3b14f))
* suppress bot-blocked-by-user error in AuthMiddleware ([fda9f3b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fda9f3beecbfcca4d7abc16cf661d5ad5e3b5141))
* UnboundLocalError for get_logo_media in required_sub_channel_check ([d3c14ac](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d3c14ac30363839d1340129f279a7a7b4b021ed1))
* use traffic topup config and add WATA 429 retry ([b5998ea](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b5998ea9d22644ed2914b0e829b3a76a32a69ddf))


### Refactoring

* remove modem functionality from classic subscriptions ([ee2e79d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ee2e79db3114fe7a9852d2cd33c4b4fbbde311ea))

## [3.10.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.10.0...v3.10.1) (2026-02-11)


### Bug Fixes

* address review issues in backup, updates, and webhook handlers ([2094886](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/20948869902dc570681b05709ac8d51996330a6e))
* allow purchase when recalculated price is lower than cached ([19dabf3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/19dabf38512ae0c2121108d0b92fc8f384292484))
* change CryptoBot URL priority to bot_invoice_url for Telegram opening ([3193ffb](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3193ffbd1bee07cb79824d87cb0f77b473b22989))
* clear subscription data when user deleted from Remnawave panel ([b0fd38d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b0fd38d60c22247a0086c570665b92c73a060f2f))
* downgrade Telegram timeout errors to warning in monitoring service ([e43a8d6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e43a8d6ce4c40a7212bf90644f82da109717bdcb))
* expand backup coverage to all 68 models and harden restore ([02e40bd](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/02e40bd6f7ef8e653cae53ccd127f2f79009e0d4))
* handle nullable traffic_limit_gb and end_date in subscription model ([e94b93d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e94b93d0c10b4e61d7750ca47e1b2f888f5873ed))
* handle StaleDataError in webhook when user already deleted ([d58a80f](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d58a80f3eaa64a6fc899e10b3b14584fb7fc18a9))
* ignore 'message is not modified' on privacy policy decline ([be1da97](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/be1da976e14a35e6cca01a7fca7529c55c1a208b))
* preserve purchased traffic when extending same tariff ([b167ed3](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/b167ed3dd1c6e6239db2bdbb8424bcb1fb7715d9))
* prevent cascading greenlet errors after sync rollback ([a1ffd5b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a1ffd5bda6b63145104ce750835d8e6492d781dc))
* protect server counter callers and fix tariff change detection ([bee4aa4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/bee4aa42842b8b6611c7c268bcfced408a227bc0))
* suppress 'message is not modified' error in updates panel ([3a680b4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/3a680b41b0124848572809d187cab720e1db8506))
* use callback fallback when MINIAPP_CUSTOM_URL is not set ([eaf3a07](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/eaf3a07579729031030308d77f61a5227b796c02))
* use flush instead of commit in server counter functions ([6cec024](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6cec024e46ef9177cb59aa81590953c9a75d81bb))

## [3.10.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.9.1...v3.10.0) (2026-02-10)


### New Features

* add all remaining RemnaWave webhook events (node, service, crm, device) ([1e37fd9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1e37fd9dd271814e644af591343cada6ab12d612))
* add close button to all webhook notifications ([d9de15a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/d9de15a5a06aec3901415bdfd25b55d2ca01d28c))
* add MULENPAY_WEBSITE_URL setting for post-payment redirect ([fe5f5de](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe5f5ded965e36300e1c73f25f16de22f84651ad))
* add RemnaWave incoming webhooks for real-time subscription events ([6d67cad](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/6d67cad3e7aa07b8490d88b73c38c4aca6b9e315))
* handle errors.bandwidth_usage_threshold_reached_max_notifications webhook ([8e85e24](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/8e85e244cb786fb4c06162f2b98d01202e893315))
* handle service.subpage_config_changed webhook event ([43a326a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/43a326a98ccc3351de04d9b2d660d3e7e0cb0efc))
* unified notification delivery for webhook events (email + WS support) ([26637f0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/26637f0ae5c7264c0430487d942744fd034e78e8))
* webhook protection — prevent sync/monitoring from overwriting webhook data ([184c52d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/184c52d4ea3ce02d40cf8a5ab42be855c7c7ae23))


### Bug Fixes

* add action buttons to webhook notifications and fix empty device names ([7091eb9](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7091eb9c148aaf913c4699fc86fef5b548002668))
* add missing placeholders to Arabic SUBSCRIPTION_INFO template ([fe54640](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/fe546408857128649930de9473c7cde1f7cc450a))
* allow non-HTTP deep links in crypto link webhook updates ([f779225](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f77922522a85b3017be44b5fc71da9c95ec16379))
* build composite device name from platform + hwid short suffix ([17ce640](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/17ce64037f198837c8f2aa7bf863871f60bdf547))
* downgrade transient API errors (502/503/504) to warning level ([ec8eaf5](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ec8eaf52bfdc2bde612e4fc0324575ba7dc6b2e1))
* extract device name from nested hwidUserDevice object ([79793c4](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/79793c47bbbdae8b0f285448d5f70e90c9d4f4b0))
* preserve payment initiation time in transaction created_at ([90d9df8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/90d9df8f0e949913f09c4ebed8fe5280453ab3ab))
* security and architecture fixes for webhook handlers ([dc1e96b](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/dc1e96bbe9b4496e91e9dea591c7fc0ef4cc245b))
* stop CryptoBot webhook retry loop and save cabinet payments to DB ([2cb6d73](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/2cb6d731e96cbfc305b098d8424b84bfd6826fb4))
* sync subscription status from panel in user.modified webhook ([5156d63](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/5156d635f0b5bc0493e8f18ce9710cca6ff4ffc8))
* use event field directly as event_name (already includes scope prefix) ([9aa22af](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/9aa22af3390a249d1b500d75a7d7189daaed265e))
* webhook:close button not working due to channel check timeout ([019fbc1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/019fbc12b6cf61d374bbed4bce3823afc60445c9))

## [3.9.1](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.9.0...v3.9.1) (2026-02-10)


### Bug Fixes

* don't delete Heleket invoice message on status check ([9943253](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/994325360ca7665800177bfad8f831154f4d733f))
* safe HTML preview truncation and lazy-load subscription fallback ([40d8a6d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/40d8a6dc8baf3f0f7c30b0883898b4655a907eb5))
* use actual DB columns for subscription fallback query ([f0e7f8e](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/f0e7f8e3bec27d97a3f22445948b8dde37a92438))

## [3.9.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v3.8.0...v3.9.0) (2026-02-09)


### New Features

* add lite mode functionality with endpoints for retrieval and update ([7b0403a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/7b0403a307702c24efefc5c14af8cb2fb7525671))
* add Persian (fa) locale with complete translations ([29a3b39](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/29a3b395b6e67e4ce2437b75120b78c76b69ff4f))
* allow tariff deletion with active subscriptions ([ebd6bee](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/ebd6bee05ed7d9187de9394c64dfd745bb06b65a))
* **localization:** add Persian (fa) locale support and wire it across app flows ([cc54a7a](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/cc54a7ad2fb98fe6e662e1923027f4989ae72868))


### Bug Fixes

* nullify payment FK references before deleting transactions in user restoration ([0b86f37](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/0b86f379b4e55e499ca3d189137e2aed865774b5))
* prevent sync from overwriting end_date for non-ACTIVE panel users ([49871f8](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/49871f82f37d84979ea9ec91055e3f046d5854be))
* promo code max_uses=0 conversion and trial UX after promo activation ([1cae713](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/1cae7130bc87493ab8c7691b3c22ead8189dab55))
* skip users with active subscriptions in admin inactive cleanup ([e79f598](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/e79f598d17ffa76372e6f88d2a498accf8175c76))
* use selection.period.days instead of selection.period_days ([4541016](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/45410168afe683675003a1c41c17074a54ce04f1))


### Performance

* cache logo file_id to avoid re-uploading on every message ([142ff14](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/142ff14a502e629446be7d67fab880d12bee149d))


### Refactoring

* remove "both" mode from BOT_RUN_MODE, keep only polling and webhook ([efa3a5d](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/efa3a5d4579f24dabeeba01a4f2e981144dd6022))
* remove Flask, use FastAPI exclusively for all webhooks ([119f463](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/119f463c36a95685c3bc6cdf704e746b0ba20d56))
* remove smart auto-activation & activation prompt, fix production bugs ([a3903a2](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/a3903a252efdd0db4b42ca3fd6771f1627050a7f))

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
