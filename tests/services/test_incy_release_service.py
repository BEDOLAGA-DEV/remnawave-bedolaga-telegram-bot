import pytest

from app.services import incy_release_service as svc


def _fake_release():
    base = 'https://github.com/INCY-DEV/incy-platforms/releases/download/desktop-v3.2.3'
    names = [
        'incy-windows-setup.exe',
        'incy-macos-arm64.dmg',
        'incy-macos-intel.dmg',
        'incy-linux-arm64.deb',
        'incy-linux-arm64.rpm',
        'incy-linux-arm64-portable.zip',
        'incy-linux-x64.deb',
        'incy-linux-x64.rpm',
        'incy-linux-x64-portable.zip',
        'some-unrelated-asset.txt',
    ]
    return {
        'tag_name': 'desktop-v3.2.3',
        'assets': [{'name': n, 'browser_download_url': f'{base}/{n}'} for n in names],
    }


def test_build_asset_map_matches_known_filenames():
    m = svc._build_asset_map(_fake_release())
    assert m['windows'].endswith('incy-windows-setup.exe')
    assert m['macos:arm'].endswith('incy-macos-arm64.dmg')
    assert m['macos:intel'].endswith('incy-macos-intel.dmg')
    assert m['linux:arm:deb'].endswith('incy-linux-arm64.deb')
    assert m['linux:arm:rpm'].endswith('incy-linux-arm64.rpm')
    assert m['linux:arm:portable'].endswith('incy-linux-arm64-portable.zip')
    assert m['linux:x64:deb'].endswith('incy-linux-x64.deb')
    assert m['linux:x64:rpm'].endswith('incy-linux-x64.rpm')
    assert m['linux:x64:portable'].endswith('incy-linux-x64-portable.zip')
    # Unrelated asset is ignored
    assert all('unrelated' not in v for v in m.values())


def test_build_asset_map_empty_on_missing_assets():
    assert svc._build_asset_map({'assets': []}) == {}
    assert svc._build_asset_map({}) == {}


@pytest.mark.asyncio
async def test_get_incy_desktop_assets_caches_and_falls_back(monkeypatch):
    svc._reset_cache_for_tests()
    calls = {'n': 0}

    async def fake_fetch():
        calls['n'] += 1
        return _fake_release()

    monkeypatch.setattr(svc, '_fetch_latest_release_json', fake_fetch)

    m1 = await svc.get_incy_desktop_assets()
    m2 = await svc.get_incy_desktop_assets()  # served from cache
    assert m1 == m2
    assert m1['windows'].endswith('incy-windows-setup.exe')
    assert calls['n'] == 1  # second call hit the cache

    # On fetch failure, the stale cache is returned (no crash)
    async def boom():
        raise RuntimeError('github down')

    monkeypatch.setattr(svc, '_fetch_latest_release_json', boom)
    svc._expire_cache_for_tests()
    m3 = await svc.get_incy_desktop_assets()
    assert m3 == m1


@pytest.mark.asyncio
async def test_get_incy_desktop_assets_empty_when_no_cache_and_fetch_fails(monkeypatch):
    svc._reset_cache_for_tests()

    async def boom():
        raise RuntimeError('github down')

    monkeypatch.setattr(svc, '_fetch_latest_release_json', boom)
    assert await svc.get_incy_desktop_assets() == {}
