from app.keyboards.inline import (
    get_app_choice_keyboard,
    get_incy_download_platform_keyboard,
    get_incy_download_macos_keyboard,
    get_incy_download_linux_arch_keyboard,
    get_incy_download_linux_pkg_keyboard,
    get_incy_download_link_keyboard,
)


def _all_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_app_choice_keyboard_has_happ_and_incy_with_sub_id():
    kb = get_app_choice_keyboard('ru', sub_id=7)
    cbs = _all_callbacks(kb)
    assert 'nz!_capp:happ:7' in cbs
    assert 'nz!_capp:incy:7' in cbs


def test_app_choice_keyboard_without_sub_id():
    kb = get_app_choice_keyboard('ru', sub_id=None)
    cbs = _all_callbacks(kb)
    assert 'nz!_capp:happ' in cbs
    assert 'nz!_capp:incy' in cbs


def test_incy_platform_keyboard_callbacks():
    cbs = _all_callbacks(get_incy_download_platform_keyboard('ru'))
    for c in ['nz!_incy_dl:android', 'nz!_incy_dl:ios', 'nz!_incy_dl:windows',
              'nz!_incy_dl:macos', 'nz!_incy_dl:linux']:
        assert c in cbs


def test_incy_macos_keyboard_callbacks():
    cbs = _all_callbacks(get_incy_download_macos_keyboard('ru'))
    assert 'nz!_incy_dl:macos:arm' in cbs
    assert 'nz!_incy_dl:macos:intel' in cbs


def test_incy_linux_arch_and_pkg_callbacks():
    arch_cbs = _all_callbacks(get_incy_download_linux_arch_keyboard('ru'))
    assert 'nz!_incy_dl:linux:arm' in arch_cbs
    assert 'nz!_incy_dl:linux:x64' in arch_cbs

    pkg_cbs = _all_callbacks(get_incy_download_linux_pkg_keyboard('ru', 'x64'))
    assert 'nz!_incy_dl:linux:x64:deb' in pkg_cbs
    assert 'nz!_incy_dl:linux:x64:rpm' in pkg_cbs
    assert 'nz!_incy_dl:linux:x64:portable' in pkg_cbs


def test_incy_link_keyboard_has_url_button():
    kb = get_incy_download_link_keyboard('ru', 'https://example/file.dmg')
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert 'https://example/file.dmg' in urls
