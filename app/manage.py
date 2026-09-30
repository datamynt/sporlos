"""Enkel admin-CLI for dev/dogfood.

    python -m app.manage init
    python -m app.manage create-site "Datamynt" merdata.no
    python -m app.manage seo-sync [dager]      # GSC/Bing → search_stats (cron: daglig)
    python -m app.manage retention [dager]     # delete raw events older than 90 days (cron: daily)
    python -m app.manage mask-paths [--apply] [--backup-dir DIR]   # mask stored ID-like paths
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
        # Normally the first event after midnight has already done this.
        print(f"salts: {store.purge_old_salts()} old row(s) deleted")
        return 0

    if cmd == "blind-hashes":
        # One-off after the switch to random daily salts. Dry run without --apply.
        res = store.blind_legacy_hashes(apply="--apply" in argv[1:])
        # The count is every event before today: the command can't tell a blinded
        # hash from a legacy one, so a dry run after --apply shows the same number.
        verb = "blinded" if res["applied"] else "would process (dry run, pass --apply)"
        print(f"blind-hashes: {verb} {res['events']} events across {res['days']} days")
        return 0

    if cmd == "retention":
        days = int(argv[1]) if len(argv) > 1 else 90
        deleted, sealed = store.retention_sweep(days)
        print(f"retention: {deleted} events slettet (eldre enn {days} d), "
              f"{sealed} dager forseglet først")
        return 0

    if cmd == "mask-paths":
        # One-off for events stored before ingest masked ID-like segments. Dry run
        # without --apply; --apply writes a 0600 CSV backup (id + old value) first.
        from app import mask_stored
        args = argv[1:]
        backup_dir = "backups"
        if "--backup-dir" in args:
            i = args.index("--backup-dir")
            if i + 1 >= len(args):
                print("usage: mask-paths [--apply] [--backup-dir DIR]")
                return 2
            backup_dir = args[i + 1]
        res = mask_stored.run(apply="--apply" in args, backup_dir=backup_dir)
        rows = ", ".join(f"{k}: {v}" for k, v in sorted(res["rows"].items())) or "nothing to mask"
        verb = "masked" if res["applied"] else "would mask (dry run, pass --apply)"
        print(f"mask-paths: {verb}: {rows}")
        for ex in res["examples"]:
            print(f"  e.g. {ex}")
        if res["backup"]:
            print(f"  backup: {res['backup']} (0600, id + old value)")
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
