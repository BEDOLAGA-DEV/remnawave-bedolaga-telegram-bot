"""Trials/pricing/reviews/offers resolve to their own sections, not the bundled ones."""
from app.database.crud.bot_role import BOT_ROLE_SECTIONS
from app.middlewares.admin_permission import resolve_admin_section as r


def test_new_sections_registered():
    for s in ('trials', 'pricing', 'reviews', 'offers'):
        assert s in BOT_ROLE_SECTIONS


def test_features_map_to_new_sections():
    assert r('admin_trials') == 'trials'
    assert r('admin_trials_reset') == 'trials'
    assert r('admin_pricing') == 'pricing'
    assert r('admin_pricing_edit:5') == 'pricing'
    assert r('admin_subs_pricing') == 'pricing'          # alias, must win over admin_subs_
    assert r('admin_reviews') == 'reviews'
    assert r('admin_review_approve_5') == 'reviews'
    assert r('admin_scheduled_promos') == 'offers'
    assert r('spromo_view:3') == 'offers'
    assert r('spromo_delete_confirm:3') == 'offers'


def test_labels_present():
    from app.handlers.admin.bot_roles import SECTION_LABELS
    for s in ('trials', 'pricing', 'reviews', 'offers'):
        assert s in SECTION_LABELS
