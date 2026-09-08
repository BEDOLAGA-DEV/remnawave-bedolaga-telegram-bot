"""Публичный эндпоинт отписки от маркетинговых писем.

Намеренно БЕЗ авторизации: по ссылке из письма ходят и почтовые клиенты
(Gmail дёргает POST сам, по RFC 8058), и пользователи, которые давно вышли из
кабинета. Требовать вход — значит гарантированно получить жалобу «Спам»
вместо отписки.

Состояние меняет ТОЛЬКО ``POST``. Корпоративные почтовые шлюзы (Defender Safe
Links, Proofpoint URL Defense, Mimecast) при доставке сами дёргают GET по каждой
ссылке из письма — если бы отписывал GET, такой шлюз отписывал бы получателя
ещё до того, как тот открыл письмо, и целые домены уходили бы в ноль. Сделать
повтор идемпотентным тут не помогает: коммитит саму отписку первый же запрос
сканера.

При этом лишнего клика у человека не появляется: ``GET`` отдаёт страницу,
которая сама отправляет форму (сканеры JS не исполняют и POST не шлют), а без
JS остаётся обычная кнопка. Gmail/Yahoo по RFC 8058 шлют POST напрямую и этой
страницы не видят.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.services.email_unsubscribe import apply_unsubscribe
from app.config import settings
from app.localization.texts import get_texts


router = APIRouter(prefix='/public', tags=['Cabinet:Public'])


def _page(title: str, message: str, *, ok: bool) -> HTMLResponse:
    """Самодостаточная страница: письма читают где угодно, внешние ресурсы не тянем."""
    service_name = html.escape(settings.SMTP_FROM_NAME or 'VPN')
    cabinet_url = (getattr(settings, 'CABINET_URL', '') or '').strip()
    accent = '#16a34a' if ok else '#dc2626'
    link_text = get_texts().t('CABINET_UNSUBSCRIBE_SETTINGS_LINK', 'Настройки уведомлений в личном кабинете')
    link = (
        f'<p style="margin:22px 0 0"><a href="{html.escape(cabinet_url, quote=True)}" '
        f'style="color:{accent}">{link_text}</a></p>'
        if cabinet_url
        else ''
    )
    body = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
</head>
<body style="margin:0;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2933">
  <div style="max-width:520px;margin:12vh auto;padding:34px 30px;background:#fff;border:1px solid #e6e8ec;border-top:3px solid {accent};border-radius:16px">
    <h1 style="margin:0 0 12px;font-size:21px;color:#0a0f1a">{html.escape(title)}</h1>
    <p style="margin:0;font-size:15px;line-height:1.6">{html.escape(message)}</p>
    {link}
    <p style="margin:26px 0 0;font-size:12px;color:#98a2b3">&copy; {service_name}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


async def _unsubscribe(token: str, db: AsyncSession) -> bool:
    if not getattr(settings, 'EMAIL_UNSUBSCRIBE_ENABLED', True):
        return False
    return await apply_unsubscribe(db, token)


def _auto_submit_page(token: str) -> HTMLResponse:
    """Страница, которая сама отправляет POST. Без JS — обычная кнопка."""
    safe_token = html.escape(token, quote=True)
    service_name = html.escape(settings.SMTP_FROM_NAME or 'VPN')
    page_title = get_texts().t('CABINET_UNSUBSCRIBE_PAGE_TITLE', 'Отписка')
    heading = get_texts().t('CABINET_UNSUBSCRIBE_HEADING', 'Отписка от рассылок')
    noscript_hint = get_texts().t('CABINET_UNSUBSCRIBE_NOSCRIPT_HINT', 'Нажмите кнопку, чтобы подтвердить отписку.')
    button_label = get_texts().t('CABINET_UNSUBSCRIBE_BUTTON', 'Отписаться')
    body = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{page_title}</title>
</head>
<body style="margin:0;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2933">
  <div style="max-width:520px;margin:12vh auto;padding:34px 30px;background:#fff;border:1px solid #e6e8ec;border-radius:16px">
    <h1 style="margin:0 0 12px;font-size:21px;color:#0a0f1a">{heading}</h1>
    <form id="u" method="post" action="?token={safe_token}">
      <noscript><p style="margin:0 0 18px;font-size:15px">{noscript_hint}</p></noscript>
      <button type="submit" style="border:0;border-radius:10px;padding:12px 20px;font-size:15px;background:#dc2626;color:#fff;cursor:pointer">
        {button_label}
      </button>
    </form>
    <p style="margin:26px 0 0;font-size:12px;color:#98a2b3">&copy; {service_name}</p>
  </div>
  <script>document.getElementById('u').submit();</script>
</body>
</html>"""
    return HTMLResponse(body)


@router.get(
    '/unsubscribe',
    summary=get_texts().t('CABINET_UNSUBSCRIBE_PAGE_SUMMARY', 'Страница отписки (ссылка из письма)'),
)
async def unsubscribe_page(token: str = '') -> HTMLResponse:
    """Ничего не меняет — только отдаёт самоотправляющуюся форму.

    БД тут намеренно не трогаем: этот GET выполняют почтовые сканеры.
    """
    return _auto_submit_page(token)


@router.post(
    '/unsubscribe',
    summary=get_texts().t('CABINET_UNSUBSCRIBE_ONE_CLICK_SUMMARY', 'Отписка (one-click RFC 8058 и форма со страницы)'),
)
async def unsubscribe_one_click(
    request: Request,
    token: str = '',
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    """Единственное место, где отписка применяется.

    Сюда приходят и кнопка «Отписаться» в Gmail/Yahoo, и форма со страницы выше.
    Браузеру отвечаем страницей с результатом, почтовому клиенту — пустым 200:
    ему всё равно нечего показать пользователю, а на протухшем токене ошибка
    только напугала бы.
    """
    ok = await _unsubscribe(token, db)

    if 'text/html' not in (request.headers.get('accept') or ''):
        return Response(status_code=200)

    if ok:
        return _page(
            get_texts().t('CABINET_UNSUBSCRIBE_SUCCESS_TITLE', 'Вы отписались'),
            get_texts().t(
                'CABINET_UNSUBSCRIBE_SUCCESS_MESSAGE',
                'Больше не будем присылать новости и промо-предложения на этот адрес. '
                'Письма по вашей подписке — оплата, продление, доступ — продолжат приходить.',
            ),
            ok=True,
        )
    return _page(
        get_texts().t('CABINET_UNSUBSCRIBE_ERROR_TITLE', 'Ссылка не сработала'),
        get_texts().t(
            'CABINET_UNSUBSCRIBE_ERROR_MESSAGE',
            'Ссылка устарела или адрес почты изменился. Отключить рассылки можно в '
            'настройках уведомлений личного кабинета.',
        ),
        ok=False,
    )
