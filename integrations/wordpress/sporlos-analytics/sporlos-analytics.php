<?php
/**
 * Plugin Name: Sporløs Analytics
 * Plugin URI: https://sporlos.no
 * Description: Cookieløs webanalyse fra Sporløs, uten samtykkebanner. Legger inn sporingskoden på alle offentlige sider.
 * Version: 1.1.0
 * Author: Datamynt AS
 * Author URI: https://datamynt.no
 * License: GPLv2 or later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain: sporlos-analytics
 * Requires at least: 6.3
 * Requires PHP: 7.4
 * WC requires at least: 8.0
 * WC tested up to: 11.2
 */

if (!defined('ABSPATH')) {
    exit; // Direktekall stoppes.
}

const SPORLOS_DEFAULT_BASE = 'https://sporlos.no';

/** Innstillinger: Innstillinger → Sporløs */
function sporlos_register_settings()
{
    register_setting('sporlos', 'sporlos_site_id', [
        'type' => 'string',
        'sanitize_callback' => 'sporlos_sanitize_site_id',
        'default' => '',
    ]);
    register_setting('sporlos', 'sporlos_api_base', [
        'type' => 'string',
        'sanitize_callback' => 'esc_url_raw',
        'default' => SPORLOS_DEFAULT_BASE,
    ]);
    register_setting('sporlos', 'sporlos_track_logged_in', [
        'type' => 'boolean',
        'sanitize_callback' => 'rest_sanitize_boolean',
        'default' => false,
    ]);
    // WooCommerce-kjøp: på som standard (gjør ingenting uten WooCommerce).
    register_setting('sporlos', 'sporlos_woo_track', [
        'type' => 'boolean',
        'sanitize_callback' => 'rest_sanitize_boolean',
        'default' => true,
    ]);
    register_setting('sporlos', 'sporlos_woo_revenue_tax', [
        'type' => 'string',
        'sanitize_callback' => 'sporlos_sanitize_revenue_tax',
        'default' => 'incl',
    ]);
}
add_action('admin_init', 'sporlos_register_settings');

function sporlos_sanitize_site_id($v)
{
    // Site-ID er URL-safe base64-aktig (a-z A-Z 0-9 _ -), maks 32 tegn.
    return substr(preg_replace('/[^A-Za-z0-9_-]/', '', (string) $v), 0, 32);
}

function sporlos_sanitize_revenue_tax($v)
{
    return $v === 'excl' ? 'excl' : 'incl';
}

function sporlos_settings_menu()
{
    add_options_page('Sporløs', 'Sporløs', 'manage_options', 'sporlos', 'sporlos_settings_page');
}
add_action('admin_menu', 'sporlos_settings_menu');

function sporlos_settings_page()
{
    if (!current_user_can('manage_options')) {
        return;
    }
    ?>
    <div class="wrap">
      <h1>Sporløs Analytics</h1>
      <p>Cookieløs webanalyse uten cookie-banner. Finn site-ID-en din i
         <a href="https://sporlos.no/app" target="_blank" rel="noopener">Sporløs-dashbordet</a>
         under «Vis sporings-kode».</p>
      <form action="options.php" method="post">
        <?php settings_fields('sporlos'); ?>
        <table class="form-table" role="presentation">
          <tr>
            <th scope="row"><label for="sporlos_site_id">Site-ID</label></th>
            <td><input name="sporlos_site_id" id="sporlos_site_id" type="text" class="regular-text"
                       value="<?php echo esc_attr(get_option('sporlos_site_id', '')); ?>"
                       placeholder="f.eks. 6LIACtOSP-S7"></td>
          </tr>
          <tr>
            <th scope="row"><label for="sporlos_api_base">Sporløs-server</label></th>
            <td><input name="sporlos_api_base" id="sporlos_api_base" type="url" class="regular-text"
                       value="<?php echo esc_attr(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE)); ?>">
                <p class="description">La stå som standard med mindre du self-hoster Sporløs.</p></td>
          </tr>
          <tr>
            <th scope="row">Innloggede brukere</th>
            <td><label><input name="sporlos_track_logged_in" type="checkbox" value="1"
                       <?php checked(get_option('sporlos_track_logged_in', false)); ?>>
                Mål også innloggede brukere (vanligvis av, så egne redaktører ikke telles)</label></td>
          </tr>
          <tr>
            <th scope="row"><?php echo esc_html__('WooCommerce', 'sporlos-analytics'); ?></th>
            <td><label><input name="sporlos_woo_track" type="checkbox" value="1"
                       <?php checked(get_option('sporlos_woo_track', true)); ?>>
                <?php echo esc_html__('Spor WooCommerce-kjøp', 'sporlos-analytics'); ?></label>
                <p class="description"><?php
                    echo esc_html__('Sender beløp, valuta, produktnavn, antall og betalingsmåte én gang per betalt ordre. Aldri ordrenummer, e-post, navn eller adresse.', 'sporlos-analytics');
                    if (!sporlos_woo_active()) {
                        echo ' ' . esc_html__('(WooCommerce er ikke aktivt på dette nettstedet.)', 'sporlos-analytics');
                    }
                ?></p></td>
          </tr>
          <tr>
            <th scope="row"><label for="sporlos_woo_revenue_tax"><?php echo esc_html__('Omsetning med eller uten mva', 'sporlos-analytics'); ?></label></th>
            <td><select name="sporlos_woo_revenue_tax" id="sporlos_woo_revenue_tax">
                  <option value="incl" <?php selected(get_option('sporlos_woo_revenue_tax', 'incl'), 'incl'); ?>><?php echo esc_html__('Med mva (ordresum slik kunden betaler)', 'sporlos-analytics'); ?></option>
                  <option value="excl" <?php selected(get_option('sporlos_woo_revenue_tax', 'incl'), 'excl'); ?>><?php echo esc_html__('Uten mva', 'sporlos-analytics'); ?></option>
                </select></td>
          </tr>
        </table>
        <?php submit_button(); ?>
      </form>
    </div>
    <?php
}

/** Snippet på offentlige sider — enqueued etter WP-reglene, defer-strategi. */
function sporlos_enqueue_script()
{
    $site_id = get_option('sporlos_site_id', '');
    if ($site_id === '') {
        return;
    }
    if (is_user_logged_in() && !get_option('sporlos_track_logged_in', false)) {
        return;
    }
    $base = rtrim(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE), '/');
    wp_enqueue_script(
        'sporlos-analytics',
        $base . '/sporlos.js',
        array(),
        null, // ekstern, versjonsløs — cache styres av Sporløs-serveren
        array('strategy' => 'defer', 'in_footer' => false)
    );
}
add_action('wp_enqueue_scripts', 'sporlos_enqueue_script');

/** Trackeren leser data-site/data-api fra sin egen script-tag — legg dem på. */
function sporlos_script_attributes($tag, $handle)
{
    if ($handle !== 'sporlos-analytics') {
        return $tag;
    }
    $site_id = get_option('sporlos_site_id', '');
    $base = rtrim(get_option('sporlos_api_base', SPORLOS_DEFAULT_BASE), '/');
    return str_replace(
        ' src=',
        sprintf(' data-site="%s" data-api="%s" src=', esc_attr($site_id), esc_url($base . '/api/event')),
        $tag
    );
}
add_filter('script_loader_tag', 'sporlos_script_attributes', 10, 2);

/** Lenke til innstillinger fra plugin-listen. */
function sporlos_action_links($links)
{
    array_unshift($links, '<a href="' . admin_url('options-general.php?page=sporlos') . '">Innstillinger</a>');
    return $links;
}
add_filter('plugin_action_links_' . plugin_basename(__FILE__), 'sporlos_action_links');

/* ------------------------------------------------------------------ *
 * WooCommerce: ett kjøp per betalt ordre, fra «takk for bestillingen»-siden.
 * Sendes som sporlos('purchase', {...}) — kun beløp, valuta, produktnavn,
 * antall og en kort betalingsmåte-slug. Aldri ordrenummer eller kundedata.
 * ------------------------------------------------------------------ */

/** Kjøres på woocommerce_thankyou-siden. Sist beregnede kjøp til wp_footer. */
function sporlos_woo_pending($set = null)
{
    static $pending = null;
    if ($set !== null) {
        $pending = $set;
    }
    return $pending;
}

function sporlos_woo_active()
{
    return class_exists('WooCommerce') && function_exists('wc_get_order');
}

/** Deklarer HPOS-kompatibilitet (vi bruker bare WC-ordre-API-et). */
function sporlos_woo_declare_compat()
{
    if (class_exists('\\Automattic\\WooCommerce\\Utilities\\FeaturesUtil')) {
        \Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);
    }
}
add_action('before_woocommerce_init', 'sporlos_woo_declare_compat');

/** Betalingsmåte-slug i trackerens format: [a-z0-9_-], maks 32 tegn. */
function sporlos_woo_payment_slug($gateway_id)
{
    $slug = strtolower(preg_replace('/[^A-Za-z0-9_-]/', '_', (string) $gateway_id));
    return substr($slug, 0, 32);
}

/** Produktnavn fra katalogen — uten variasjons-attributter. */
function sporlos_woo_item_name($item)
{
    $name = $item->get_name();
    $product = $item->get_product();
    if ($product) {
        if ($product->is_type('variation') && $product->get_parent_id()) {
            $parent = wc_get_product($product->get_parent_id());
            $name = $parent ? $parent->get_name() : $name;
        } else {
            $name = $product->get_name();
        }
    }
    $name = wp_specialchars_decode(wp_strip_all_tags((string) $name), ENT_QUOTES);
    return mb_substr(trim($name), 0, 160);
}

/** Bygger kjøpet for en ordre, eller null hvis den ikke skal telles. */
function sporlos_woo_build_purchase($order)
{
    $incl_tax = get_option('sporlos_woo_revenue_tax', 'incl') !== 'excl';

    $revenue = (float) $order->get_total();
    if (!$incl_tax) {
        $revenue -= (float) $order->get_total_tax();
    }
    if ($revenue <= 0) {
        return null; // gratisordre er ikke et kjøp
    }

    $items = array();
    foreach ($order->get_items() as $item) {
        $qty = max(1, (int) $item->get_quantity());
        $line = (float) $item->get_total();
        if ($incl_tax) {
            $line += (float) $item->get_total_tax();
        }
        $name = sporlos_woo_item_name($item);
        if ($name === '') {
            continue;
        }
        $items[] = array(
            'name' => $name,
            'qty' => $qty,
            'price' => round(max(0, $line) / $qty, 2),
        );
    }

    $purchase = array('revenue' => round($revenue, 2));
    $currency = strtoupper((string) $order->get_currency());
    if (preg_match('/^[A-Z]{3}$/', $currency)) {
        $purchase['currency'] = $currency;
    }
    $payment = sporlos_woo_payment_slug($order->get_payment_method());
    if ($payment !== '') {
        $purchase['payment'] = $payment;
    }
    if ($items) {
        $purchase['items'] = $items;
    }
    return $purchase;
}

function sporlos_woo_thankyou($order_id)
{
    if (!sporlos_woo_active() || !get_option('sporlos_woo_track', true)) {
        return;
    }
    // Samme regler som trackeren: uten site-ID, eller for innloggede (med mindre
    // slått på), måles ingenting — og ordren markeres ikke, så den ikke "brukes opp".
    if (get_option('sporlos_site_id', '') === '') {
        return;
    }
    if (is_user_logged_in() && !get_option('sporlos_track_logged_in', false)) {
        return;
    }
    $order = wc_get_order($order_id);
    if (!$order instanceof WC_Order) {
        return;
    }
    // Bare betalte ordrer (processing/completed) — ikke pending, failed, on-hold, cancelled.
    if (!$order->is_paid()) {
        return;
    }
    // Aldri to ganger: last inn «takk»-siden på nytt gir ikke nytt kjøp.
    if ($order->get_meta('_sporlos_tracked', true)) {
        return;
    }
    $purchase = sporlos_woo_build_purchase($order);
    if ($purchase === null) {
        return;
    }
    $order->update_meta_data('_sporlos_tracked', '1');
    $order->save_meta_data();
    sporlos_woo_pending($purchase);
}
add_action('woocommerce_thankyou', 'sporlos_woo_thankyou', 10, 1);

/**
 * Skriver kjøpet i footer. wp_add_inline_script() duger ikke her: trackeren
 * skrives ut i <head>, og woocommerce_thankyou kjører først i innholdet, så
 * «etter»-skriptet ville aldri blitt skrevet ut. Trackeren er defer og har ingen
 * kø, så kallet venter til den er lastet (DOMContentLoaded kommer etter defer).
 */
function sporlos_woo_print_purchase()
{
    $purchase = sporlos_woo_pending();
    if (!$purchase || !wp_script_is('sporlos-analytics', 'enqueued')) {
        return;
    }
    $json = wp_json_encode($purchase, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT);
    if (!$json) {
        return;
    }
    $js = '(function(){var d=' . $json . ',done=false;'
        . 'function go(){if(done||typeof window.sporlos!=="function"){return;}done=true;window.sporlos("purchase",d);}'
        . 'go();if(!done){document.addEventListener("DOMContentLoaded",go);window.addEventListener("load",go);}})();';
    wp_print_inline_script_tag($js, array('id' => 'sporlos-purchase'));
}
add_action('wp_footer', 'sporlos_woo_print_purchase', 99);
