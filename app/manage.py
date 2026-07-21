"""Enkel admin-CLI for dev/dogfood.

    python -m app.manage init
    python -m app.manage create-site "Datamynt" merdata.no
    python -m app.manage seo-sync [dager]      # GSC/Bing → search_stats (cron: daglig)
"""

from __future__ import annotations

import os
import sys

from app import store


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = argv[0]
    if cmd == "init":
        store.init_db()
        print("db initialisert")
        return 0

    if cmd == "create-site":
        if len(argv) < 3:
            print("bruk: create-site <tenant-navn> <domene>")
            return 2
        store.init_db()
        tenant_id = store.create_tenant(argv[1])
        site = store.create_site(tenant_id, argv[2])
        # Bruk prod-domenet hvis satt, ellers localhost for lokal dev.
        domain = os.environ.get("SPORLOS_DOMAIN")
        base = f"https://{domain}" if domain and "FYLL_INN" not in domain else "http://localhost:8000"
        print(f"site opprettet: {site['domain']}")
        print(f"  public_id: {site['public_id']}")
        print(f"  snippet:   <script defer data-site=\"{site['public_id']}\" "
              f"data-api=\"{base}/api/event\" src=\"{base}/sporlos.js\"></script>")
        print(f"  dashboard: {base}/app?site={site['public_id']}")
        return 0

    if cmd == "rollup":
        day = argv[1] if len(argv) > 1 else None
        n, d = store.rollup_all(day)
        print(f"rollup kjørt for {n} sites, dag {d}")
        return 0

    if cmd == "retention":
        days = int(argv[1]) if len(argv) > 1 else 90
        deleted, sealed = store.retention_sweep(days)
        print(f"retention: {deleted} events slettet (eldre enn {days} d), "
              f"{sealed} dager forseglet først")
        return 0

    if cmd == "anchor":
        from app.anchor import anchor_pending
        print(anchor_pending())
        return 0

    if cmd == "trial-reminders":
        from app.notify import send_trial_reminders
        days = int(argv[1]) if len(argv) > 1 else 3
        print(f"trial-varsler sendt: {send_trial_reminders(days)}")
        return 0

    if cmd == "weekly-report":
        from app.notify import send_weekly_reports
        print(f"ukerapporter sendt: {send_weekly_reports()}")
        return 0

    if cmd == "overage-alerts":
        from app.notify import send_overage_alerts
        print(f"grense-varsler sendt: {send_overage_alerts()}")
        return 0

    if cmd == "stalled-alerts":
        from app.notify import send_stalled_alerts
        print(f"stille-stopp-varsler sendt: {send_stalled_alerts()}")
        return 0

    if cmd == "vipps-charges":
        from app import vipps
        print(vipps.sweep())
        return 0

    if cmd == "seo-sync":
        from app import seo
        days = int(argv[1]) if len(argv) > 1 else 30
        print(seo.sync(days))
        return 0

    if cmd == "indexnow-ping":
        from app import indexnow
        domain = os.environ.get("SPORLOS_DOMAIN")
        default = f"https://{domain}" if domain and "FYLL" not in domain else "http://localhost:8000"
        base = argv[1] if len(argv) > 1 else default
        print(indexnow.ping(base))
        return 0

    if cmd == "assist-ingest":
        from app import assist
        domain = os.environ.get("SPORLOS_DOMAIN")
        default = f"https://{domain}" if domain and "FYLL" not in domain else "http://localhost:8000"
        base = argv[1] if len(argv) > 1 else default
        print(f"assistent-kunnskap: {assist.ingest(base)} sider ingestet fra {base}")
        return 0

    if cmd == "stripe-products":
        import stripe  # noqa
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not stripe.api_key:
            print("STRIPE_SECRET_KEY mangler i miljøet")
            return 2
        plans = [("LITEN", "Sporløs Liten", 9900), ("VEKST", "Sporløs Vekst", 24900),
                 ("PRO", "Sporløs Pro", 59900)]
        for k, name, amount in plans:
            p = stripe.Product.create(name=name)
            pr = stripe.Price.create(
                product=p.id, unit_amount=amount, currency="nok",
                recurring={"interval": "month"},
            )
            print(f"STRIPE_PRICE_{k}={pr.id}")
        return 0

    print(f"ukjent kommando: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
