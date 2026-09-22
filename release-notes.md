:robot: I have created a release *beep* *boop*
---


## [4.15.0](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/compare/v4.14.0...v4.15.0) (2026-09-22)


### New Features

* **reminders:** напоминания пользователям в бот и в кабинет — условия, тексты на языках, частота, лимиты, тихие часы, админ-API и права ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **referral:** напоминания о заявках на вывод без решения ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **promo-groups:** пересчёт групп по тратам при правке групп и по кнопке в кабинете ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **promo-groups:** клиент получает уведомление об автоназначении промогруппы в Telegram или на почту ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **broadcasts:** email-рассылка по промогруппе и одному пользователю из карточки ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **grace:** настройка обнулять счётчик трафика при выдаче grace, чтобы квота читалась как 0 из 1 ГБ ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **admin:** стартовое уведомление со сводкой по разделам и блоком «требует внимания» ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **admin:** уведомление об остановке бота — причина, аптайм, что делать дальше ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))


### Bug Fixes

* **account-linking:** привязка соцсети, занятой другим аккаунтом, предлагает слияние вместо тупика ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **panel-sync:** снимок панели не укорачивает и не гасит срок, за который недавно заплатили ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **panel-sync:** связь сохраняется после пересоздания удалённой учётки панели ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **panel:** «удалил подписку и купил заново» больше не наследует удалённый аккаунт панели ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **grace:** грейс по лимиту закрывается, когда трафик сбросился по расписанию ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **broadcasts:** одно письмо на человека в email-фильтрах по подписке ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **reminders:** отписанные от маркетинга не перекрывают очередь остальным ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **reminders:** устойчивость к гонке удаления, неизвестный тип кнопки, права для ролей Admin и Marketer ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **admin/payments:** успешные автопродления Platega СБП отображаются в платежах ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **admin-chat:** кнопки тикетов и заявок на вывод работают в групповом админ-чате, нерабочие не рисуются ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **referral:** напоминание о заявке на вывод с теми же кнопками, что у исходного уведомления ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **cabinet:** покупка тарифа из кабинета спрашивает настройки сброса трафика ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **cabinet:** email-регистрация по приглашениям ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **mulenpay:** вебхук без подписи проверяется через API и не падает на некорректном теле ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **nalogo:** чек НПД по покупке с лендинга только для оплат через YooKassa ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **logging:** обрывы getUpdates не уходят в админ-чат отчётом об ошибке ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **logs:** в сообщениях логов нет следов вырезанных подстановок ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **logo:** заменённый файл логотипа показывается сразу ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))
* **branding:** иконка приложения на Android без чёрных полос по краям ([4fdcee6](https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/commit/4fdcee691ba8f4884b192ea6960d2f9ac9805422))

---
This PR was generated with [Release Please](https://github.com/googleapis/release-please). See [documentation](https://github.com/googleapis/release-please#release-please).