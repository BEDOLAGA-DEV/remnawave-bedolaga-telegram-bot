# Changelog

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
