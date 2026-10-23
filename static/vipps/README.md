Vipps' approved «Betal med Vipps» button for the plan checkout page (app/main.py, `betal`), served at
/static/vipps/vipps-betal-med-no.svg, so the page loads nothing from Vipps.

- File: vipps-betal-med-no.svg, a byte-for-byte copy of Vipps' own button (not edited): 250 x 44 px, white text on
  Vipps orange #FF5B24, Norwegian caption.
- Source: https://github.com/vippsas/vipps-woocommerce/blob/master/payment/img/vipps-rectangular-pay-NO.svg
  (Vipps MobilePay's official WooCommerce plugin; last changed in commit 797dc41d60, 2024-09-19; copied 2026-10-23).
- Why the file and not our own button: Vipps' brand guidelines say to use only the approved buttons and not change
  their design (fixed 44 px height, approved caption, font and colour):
  https://developer.vippsmobilepay.com/docs/knowledge-base/design-guidelines/ and
  https://brand.vippsmobilepay.com/document/61#/branding/online/buttons
- Licence: the repository's code licence is AGPL-3.0. The Vipps name, logo and button are trademarks of Vipps MobilePay
  AS, used as their brand guidelines allow for merchants offering Vipps.
